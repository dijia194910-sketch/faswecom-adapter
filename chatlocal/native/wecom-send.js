'use strict';
if (Process.arch !== 'ia32') throw new Error('Expected a 32-bit target');
const mod = Process.getModuleByName('WXWork.exe');
if (mod.path.toLowerCase() !== config.expectedPath.toLowerCase())
    throw new Error('Target module path differs from the checked executable');
const hex = bytes => Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
for (const [name, spec] of Object.entries(config.functions)) {
    if (hex(mod.base.add(spec.rva).readByteArray(5)) !== spec.prefix)
        throw new Error('Entry prefix mismatch: ' + name);
}
const sendEntry = mod.base.add(config.layout.conversation_vtable + 0x164).readPointer();
if (!sendEntry.equals(mod.base.add(config.functions.conversation_send.rva)))
    throw new Error('Conversation vtable mismatch');

function stringLength(address) {
    try {
        const length = address.add(16).readU32();
        const capacity = address.add(20).readU32();
        return length <= capacity && capacity < 0x1000000 ? length : null;
    } catch (_) { return null; }
}
function activeAccount(service) {
    // Interface is at primary object +0x0c; context is interface -8.
    // LoginServiceImpl slot +8 reads the current user's VID at state +0x70.
    const context = service.sub(8).readPointer();
    const holder = context.add(0x44).readPointer();
    const login = holder.add(8).readPointer();
    const state = login.add(4).readPointer();
    const first = state.add(0x70).readU64().toString();
    if (state.add(0x70).readU64().toString() !== first)
        throw new Error('ID seed changed during validation');
    return first;
}
'use strict';

// Shared object construction and lifetime management. The probe invokes this
// with a synthetic conversation and no action. The separate one-shot sender
// supplies its exact authorized conversation and submission action.
function withPendingMessage(text, conversationId, action) {
    const at = name => mod.base.add(config.functions[name].rva);
    const createPending = new NativeFunction(at('create_pending'), 'void', ['pointer'], 'mscdecl');
    const createText = new NativeFunction(at('create_text'), 'void', ['pointer', 'pointer'], 'mscdecl');
    const parseBody = new NativeFunction(at('parse_body'), 'void', ['pointer', 'uint', 'pointer'], 'mscdecl');
    const assign = new NativeFunction(at('assign_string'), 'pointer', ['pointer', 'pointer', 'uint'], 'thiscall');
    const release = new NativeFunction(at('release_shared'), 'void', ['pointer'], 'thiscall');
    const pending = Memory.alloc(8);
    const message = Memory.alloc(8);
    const body = Memory.alloc(8);
    const input = Memory.alloc(24);
    let textBytes = 0;
    for (const char of text) {
        const cp = char.codePointAt(0);
        textBytes += cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
    }
    const textBuffer = Memory.allocUtf8String(text);
    if (textBytes <= 15) input.writeUtf8String(text);
    else input.writePointer(textBuffer);
    input.add(16).writeU32(textBytes);
    input.add(20).writeU32(Math.max(15, textBytes));
    const result = {submitted: false, textBytes, released: []};
    function checkHandle(handle, name) {
        if (handle.readPointer().isNull() || handle.add(4).readPointer().isNull())
            throw new Error(name + ' factory returned an empty handle');
        return handle.readPointer();
    }
    try {
        createPending(pending);
        const p = checkHandle(pending, 'pending');
        if (p.add(16).readU32() !== 0 || p.add(20).readU32() !== 15 || p.add(24).readU32() !== 0)
            throw new Error('Pending defaults differ from the recovered layout');
        createText(message, input);
        const m = checkHandle(message, 'text');
        const type = m.add(0x50).readU32();
        if (type !== 2) throw new Error('Text factory did not produce type 2');
        const encodedBytes = stringLength(m.add(0x1b8));
        if (encodedBytes === null || encodedBytes === 0)
            throw new Error('Message body serialization is empty or invalid');
        parseBody(body, type, m.add(0x1b8));
        checkHandle(body, 'body');
        if (!/^[A-Za-z0-9_:]+$/.test(conversationId))
            throw new Error('Conversation ID is not ASCII');
        const conversation = Memory.allocUtf8String(conversationId);
        assign(p, conversation, conversationId.length);
        p.add(0x18).writeU32(type);
        // Move the newly owned protobuf pair into the default-empty pending
        // object. Its destructor will release that ownership exactly once.
        p.add(0x1c).writePointer(body.readPointer());
        p.add(0x20).writePointer(body.add(4).readPointer());
        body.writePointer(ptr(0));
        body.add(4).writePointer(ptr(0));
        result.messageType = type;
        result.encodedBytes = encodedBytes;
        result.conversationBytes = stringLength(p);
        result.payloadConstructed = true;
        if (action) Object.assign(result, action(pending));
    } finally {
        for (const [name, handle] of [['body', body], ['message', message], ['pending', pending]]) {
            if (!handle.add(4).readPointer().isNull()) {
                release(handle);
                handle.writePointer(ptr(0));
                handle.add(4).writePointer(ptr(0));
                result.released.push(name);
            }
        }
    }
    return result;
}

'use strict';

let attempted = false;
function validateNativeRequest() {
    const request = config.request;
    if (!request || !request.text || typeof request.text !== 'string' || request.text.length > 2000)
        throw new Error('Missing or invalid exact message');
    if (/[\u0000-\u0009\u000b-\u001f]/.test(request.text)) throw new Error('Unsupported control character');
    const members = request.conversation.match(/^S:(\d+)_(\d+)$/);
    if (!request.accountOnly && (!members || !members.slice(1).includes(request.account) || !members.slice(1).includes(request.recipient)))
        throw new Error('Exact direct-conversation identity mismatch');
    const manager = mod.base.add(config.layout.manager_global).readPointer();
    if (manager.isNull() || !manager.readPointer().equals(mod.base.add(config.layout.manager_vtable)))
        throw new Error('Unexpected service manager');
    const login = manager.add(0x18).readPointer();
    if (login.isNull() || !login.readPointer().equals(mod.base.add(config.layout.login_vtable)))
        throw new Error('Unexpected login interface');
    const service = manager.add(0x30).readPointer();
    if (service.isNull() || !service.readPointer().equals(mod.base.add(config.layout.message_vtable)))
        throw new Error('Unexpected message interface');
    if (!login.readPointer().add(8).readPointer().equals(mod.base.add(config.functions.get_login_vid.rva)) ||
        !service.readPointer().add(0x10).readPointer().equals(mod.base.add(config.functions.message_service_send.rva)))
        throw new Error('Native service method slot differs');
    const getVid = new NativeFunction(mod.base.add(config.functions.get_login_vid.rva),
        'uint64', ['pointer'], 'thiscall');
    const actual = getVid(login).toString();
    if (actual !== request.account || activeAccount(service) !== actual)
        throw new Error('Running account does not match the authorized conversation');
    return {account: actual, conversation: request.conversation, pid: Process.id};
}
rpc.exports.preflight = validateNativeRequest;
rpc.exports.sendonce = function () {
    if (config.request.accountOnly) throw new Error('Read-only account verification cannot send');
    if (attempted) throw new Error('This request has already been attempted; no retry');
    validateNativeRequest();
    return withPendingMessage(config.request.text, config.request.conversation, pending => {
        validateNativeRequest();
        const output = Memory.alloc(16);
        const callback = Memory.alloc(40); // Valid empty std::function, used by native callers.
        const dispatch = new NativeFunction(mod.base.add(config.functions.dispatch_send.rva),
            'void', ['pointer', 'pointer', 'pointer'], 'mscdecl');
        attempted = true;
        dispatch(output, pending, callback);
        const ids = [output.readU64().toString(), output.add(8).readU64().toString()];
        return {submitted: ids.some(id => id !== '0'), nativeCallReturned: true, ids};
    });
};
