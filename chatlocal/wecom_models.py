"""Private multi-model catalog; credentials are protected by Windows DPAPI.

Public responses contain model names, endpoints and key availability only.
Installing/selecting/editing models never makes a provider request.
"""
import base64
import json
import os
from pathlib import Path
import re
import threading
from urllib.parse import urlparse
import uuid

from .wecom_windows import protect


class ModelCatalog:
    def __init__(self,path):
        self.path=Path(path)
        self.lock=threading.RLock()

    def read(self):
        if not self.path.exists():return dict(version=1,active=None,models=[],credentials={})
        try:
            value=json.loads(self.path.read_text('utf-8'))
            if value.get('version')!=1 or not isinstance(value.get('models'),list) or not isinstance(value.get('credentials'),dict):raise ValueError()
            return value
        except (OSError,ValueError,AttributeError):
            raise ValueError('模型目录无法读取，请检查本地配置文件') from None

    def write(self,data):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temp=self.path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        try:
            temp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
            os.replace(temp,self.path)
        finally:temp.unlink(missing_ok=True)

    @staticmethod
    def seal(key):
        if not isinstance(key,str) or not 8<=len(key.strip())<=4096 or any(ord(c)<33 for c in key.strip()) or key.strip().startswith(('$','<')):
            raise ValueError('请填写真实 API Key，不能使用环境变量名或占位符')
        return base64.b64encode(protect(key.strip().encode())).decode('ascii')

    def key(self,data,provider):
        blob=data['credentials'].get(provider)
        if not blob:return ''
        try:return protect(base64.b64decode(blob,validate=True),decrypt=True).decode('utf-8')
        except (ValueError,UnicodeError):raise ValueError('此模型的密钥无法由当前 Windows 用户解密，请重新填写') from None

    def public(self):
        data=self.read();rows=[]
        for entry in data['models']:
            try:available=bool(self.key(data,entry.get('credential_id',entry['provider'])));error=''
            except ValueError as exc:available=False;error=str(exc)
            rows.append(dict(entry,enabled=entry.get('enabled',True),has_api_key=available,configured=available,key_error=error))
        return dict(active=data['active'],models=rows,managed=self.path.exists())

    @staticmethod
    def validate(models):
        if not isinstance(models,list) or not 1<=len(models)<=30:raise ValueError('模型目录范围为1–30项')
        ids=set();validated=[]
        for entry in models:
            if not isinstance(entry,dict) or set(entry)-{'id','name','model','provider','api_base','max_tokens','max_input_tokens','temperature','timeout','max_retries','supports_thinking','supports_vision','credential_id','enabled'}:
                raise ValueError('模型字段无效')
            entry=dict(entry)
            for field in ('id','name','model','provider','api_base'):
                if not isinstance(entry.get(field),str) or not entry[field] or len(entry[field])>1000 or any(ord(c)<32 for c in entry[field]):raise ValueError('模型必填字段无效')
            if not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',entry['id']) or entry['id'] in ids:raise ValueError('模型编号重复或无效')
            if entry['provider'] not in ('qwen','deepseek','openai-compatible'):raise ValueError('未知模型接口类型')
            url=urlparse(entry['api_base'])
            if url.username or url.password or url.query or url.fragment or not url.hostname or (url.scheme!='https' and not(url.scheme=='http' and url.hostname in ('localhost','127.0.0.1','::1'))):raise ValueError('模型地址须为 HTTPS 或本机 HTTP，且不能含凭据参数')
            if url.path.rstrip('/').endswith('/chat/completions'):raise ValueError('请使用 Base URL，不能包含 /chat/completions')
            entry['api_base']=entry['api_base'].rstrip('/')
            for field,low,high in (('max_tokens',1,65536),('max_input_tokens',1,4000000),('timeout',1,1800),('max_retries',0,5)):
                if field in entry and (type(entry[field]) not in (int,float) or not low<=entry[field]<=high):raise ValueError('模型参数范围无效')
            if 'temperature' in entry and (type(entry['temperature']) not in (int,float) or not 0<=entry['temperature']<=2):raise ValueError('温度范围无效')
            for field in ('supports_thinking','supports_vision'):
                if field in entry and type(entry[field]) is not bool:raise ValueError('模型能力字段必须为布尔值')
            if 'enabled' in entry and type(entry['enabled']) is not bool:raise ValueError('模型启用状态无效')
            ids.add(entry['id']);validated.append(entry)
        return validated

    def install(self,models,keys=None,active=None):
        validated=self.validate(models)
        ids={m['id'] for m in validated}
        with self.lock:
            data=self.read()
            credentials=dict(data['credentials'])
            for provider,key in (keys or {}).items():
                if provider not in {m['provider'] for m in validated}:raise ValueError('密钥对应的接口组不存在')
                credentials[provider]=self.seal(key)
            chosen=active or (data['active'] if data['active'] in ids else validated[0]['id'])
            if chosen not in ids:raise ValueError('默认模型不存在')
            self.write(dict(version=1,active=chosen,models=validated,credentials=credentials))
        return self.public()

    def select(self,model_id):
        with self.lock:
            data=self.read()
            if model_id not in {m['id'] for m in data['models']}:raise ValueError('模型不存在')
            for entry in data['models']:
                if entry['id']==model_id:entry['enabled']=True
            data['active']=model_id;self.write(data)
        return self.public()

    def set_enabled(self,model_id,enabled):
        if type(enabled) is not bool:raise ValueError('启用状态无效')
        with self.lock:
            data=self.read()
            entry=next((m for m in data['models'] if m['id']==model_id),None)
            if entry is None:raise ValueError('模型不存在')
            remaining=[m for m in data['models'] if m['id']!=model_id]
            if not enabled and not any(m.get('enabled',True) for m in remaining):
                raise ValueError('至少需要保留一个启用的模型')
            entry['enabled']=enabled
            if not enabled and data.get('active')==model_id:
                data['active']=next((m['id'] for m in remaining if m.get('enabled',True)),None)
            elif enabled and not data.get('active'):
                data['active']=model_id
            self.write(data)
        return self.public()

    def save_model(self, body):
        if set(body)-{'id','name','model','api_base','provider','api_key'}:
            raise ValueError('模型字段无效')
        model_id=body.get('id')
        with self.lock:
            data=self.read()
            old=next((m for m in data['models'] if m['id']==model_id),None)
            if model_id is not None and old is None:raise ValueError('模型不存在')
            if old is None and len(data['models'])>=30:raise ValueError('最多可保存30个模型')
            model_id=model_id or 'custom-'+uuid.uuid4().hex
            entry=dict(old or {},id=model_id,**{k:body.get(k,'').strip() if isinstance(body.get(k,''),str) else body[k]
                for k in ('name','model','api_base','provider')})
            entry=self.validate([entry])[0]
            endpoint_changed=old is None or any(entry[k]!=old[k] for k in ('provider','api_base'))
            if endpoint_changed:
                # Never reuse a provider credential when the destination changes.
                entry['credential_id']='key-'+uuid.uuid4().hex
            key=body.get('api_key','')
            if not isinstance(key,str):raise ValueError('密钥格式无效')
            if key.strip():data['credentials'][entry.get('credential_id',entry['provider'])]=self.seal(key)
            data['models']=[entry if m['id']==model_id else m for m in data['models']]
            if old is None:data['models'].append(entry)
            if not data['active']:data['active']=model_id
            self.prune_credentials(data)
            self.write(data)
        return self.public()

    @staticmethod
    def prune_credentials(data):
        used={m.get('credential_id',m['provider']) for m in data['models']}
        data['credentials']={k:v for k,v in data['credentials'].items() if k in used}

    def delete_model(self, model_id):
        with self.lock:
            data=self.read()
            if model_id not in {m['id'] for m in data['models']}:raise ValueError('模型不存在')
            data['models']=[m for m in data['models'] if m['id']!=model_id]
            if data['active']==model_id:data['active']=data['models'][0]['id'] if data['models'] else None
            self.prune_credentials(data)
            self.write(data)
        return self.public()

    def save_key(self,model_id,key):
        with self.lock:
            data=self.read();entry=next((m for m in data['models'] if m['id']==model_id),None)
            if entry is None:raise ValueError('模型不存在')
            data['credentials'][entry.get('credential_id',entry['provider'])]=self.seal(key)
            self.write(data)
        return self.public()

    def config(self,base,model_id=None):
        data=self.read()
        if not data['models']:
            if model_id:raise ValueError('模型不存在')
            return dict(base,API_KEY='',MODEL='') if self.path.exists() else dict(base)
        entry=next((m for m in data['models'] if m['id']==(model_id or data['active'])),None)
        if entry is None:raise ValueError('模型不存在')
        result=dict(base,MODEL=entry['model'],API_BASE=entry['api_base'],API_KEY=self.key(data,entry.get('credential_id',entry['provider'])),
            PROVIDER_HINT=entry['provider'],MODEL_CATALOG_ID=entry['id'],MODEL_OUTPUT_LIMIT=str(entry.get('max_tokens',64000)))
        if 'temperature' in entry:result['MODEL_TEMPERATURE']=str(entry['temperature'])
        return result