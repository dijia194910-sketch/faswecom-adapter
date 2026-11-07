"""Direct WeCom tool calls under persistent per-connection send grants."""
from .wecom_dispatch import Dispatches
from .wecom_recipients import Recipients


def call(access,grant,name,args,cancel,adapter_call):
    owner='mcp:'+grant['id']
    if name=='search_wecom_recipients':
        return Recipients(access.store).search(args['query'],access.plan(grant))
    if adapter_call is None:raise ValueError('企微发送适配器未接入当前服务，请使用企微操作台的 MCP 连接')
    dispatch=Dispatches(access.store)
    if name=='get_wecom_operation':
        permit=dispatch.get(args['dispatch_id'],owner)
        result=adapter_call('get_operation_by_key',dict(idempotency_key='dispatch-'+permit['id']))
    else:
        access.by_id(grant['id'])
        if cancel.is_set():raise ValueError('调用已取消，未发送')
        permit=dispatch.issue(args['recipient_id'],args['text'],args['idempotency_key'],owner=owner)
        access.by_id(grant['id'])
        if cancel.is_set():raise ValueError('调用已取消，未发送')
        try:result=adapter_call('send_dispatch',dict(dispatch_id=permit['id']))
        except ValueError:return dict(dispatch_id=permit['id'],state='UNKNOWN',note='未取得适配器回执，请先查询 get_wecom_operation；不要换编号重发。')
    return dict(dispatch_id=permit['id'],**(result.get('data') or dict(state='NOT_SENT',note=result.get('message','适配器拒绝请求'))))
