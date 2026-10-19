"use strict";

(() => {
  const byId = id => document.getElementById(id);

  const labels = {dashboard:"看板草稿",ticket:"工单草稿",schedule_pr:"调度变更",model_registration:"模型注册申请",notification:"企业通知",report:"定期报告"};

  const states = {pending:"待执行",executing:"执行中",completed:"已完成",cancelled:"已取消",uncertain:"结果待核对"};

  let token = "", identity = null, rows = [], cursor = null, selected = null, busy = false, generation = 0;

  const node = (tag, text, cls) => { const n=document.createElement(tag);
 if(text!==undefined)n.textContent=text;
 if(cls)n.className=cls;
 return n;
 };

  const notice = text => {byId("notice").textContent=text;
};

  function headers() {return {"authorization":`Bearer ${token}`,"content-type":"application/json","x-user-id":identity.user_id,"x-tenant-id":identity.tenant_id,"x-project-id":identity.project_id,"x-data-domain":identity.data_domains.join(",")};
}
  async function api(path, body, method) {
    const current=generation;

    // The server accepts one enrolled domain in this request header.
    const h=headers();
 h["x-data-domain"]=identity.data_domains[0] || "";

    const response=await fetch(`/v1/business/${path}`, {method:method || (body===undefined?"GET":"POST"),headers:h,body:body===undefined?undefined:JSON.stringify(body),cache:"no-store",credentials:"omit",redirect:"error"});

    if(current!==generation)throw new Error("连接已切换，请重新加载。");

    if(!response.ok){if(response.status===401){disconnect();
throw new Error("连接已失效，请重新连接。");
} throw new Error(response.status===403?"当前权限或审批不允许此操作。":response.status===409?"状态或审批已变化，请刷新后核对。":response.status===503?"接收系统暂时不可用，请先核对结果。":"请求未完成，请刷新后重试。");
}
    return response.json();

  }
  function disconnect(){generation++;
 token="";
identity=null;
rows=[];
selected=null;
cursor=null;
byId("credential").value="";
byId("workspace").hidden=true;
byId("connection").hidden=false;
byId("logout").hidden=true;
byId("drafts").replaceChildren();
byId("detail").replaceChildren();
notice("");
}
  function drawList(){const list=byId("drafts");
list.replaceChildren();
byId("count").textContent=`${rows.length} 条已加载`;
byId("more").hidden=cursor===null;
if(!rows.length)list.append(node("p","暂时没有行动草稿。生成报告或提交业务草稿后，可在这里查看。","hint"));
for(const row of rows){const i=row.intent,b=node("button",undefined,"draft");
b.type="button";
b.setAttribute("aria-pressed",String(selected===i.action_id));
b.append(node("span",i.payload.title||labels[i.payload.kind],"draft-title"));
b.append(node("span",`${labels[i.payload.kind]} · ${states[row.state]||row.state}`,"draft-meta"));
b.addEventListener("click",()=>{selected=i.action_id;
drawList();
drawDetail(row);
});
list.append(b);
}}
  function exact(section,title,value){section.append(node("h3",title));
section.append(node("pre",typeof value==="string"?value:JSON.stringify(value,null,2),"exact-content"));
}
  function drawDetail(row){const pane=byId("detail");
pane.replaceChildren();
const i=row.intent,b=i.delivery_binding;
pane.append(node("span",states[row.state]||row.state,"status"));
pane.append(node("h2",i.payload.title||labels[i.payload.kind],"detail-title"));
pane.append(node("p",i.operation==="compensate"?"本次为单独审批的补偿动作，请核对原始交付。":"执行时将核验精确审批、当前权限及接收系统状态。","description"));
const dl=node("dl");
for(const [k,v] of [["动作类型",labels[i.payload.kind]],["目标",b?.repository||b?.receiver_id||i.destination],["有效期",new Date(i.expires_at).toLocaleString("zh-CN")],["审批编号",row.approval_id]]){dl.append(node("dt",k),node("dd",v));
}pane.append(dl);
const content=node("section",undefined,"section");
if(b?.old_content!==undefined){exact(content,"变更前 · 已固定内容",b.old_content);
exact(content,"变更后 · 待提交内容",b.new_content);
}else if(b?.body!==undefined){exact(content,"提交标题",b.title);
exact(content,"提交正文",b.body);
}else if(i.payload.publication_text){exact(content,"报告正文",i.payload.publication_text);
}else{exact(content,"行动内容",i.payload);
}const details=node("details");
details.append(node("summary","查看完整请求与绑定"));
details.append(node("pre",JSON.stringify(i,null,2),"exact-content"));
content.append(details);
pane.append(content);

    if(row.receipt){const receipt=node("section",undefined,"section");
exact(receipt,"交付凭据",row.receipt);
pane.append(receipt);
pane.append(node("p",row.receipt.external_delivery?"已核验外部接收凭据。":"本地草稿已完成，尚未交付外部系统。","hint"));
}
    if(row.state==="uncertain"){pane.append(node("p","交付结果尚不确定。请核对接收系统后填写原始编号；此操作只读取结果，不会重新发送。","uncertain"));
const form=node("form",undefined,"reconcile-row"),input=node("input");
input.required=true;
input.maxLength=128;
input.setAttribute("aria-label","原始消息或工单或 PR 编号");
input.placeholder=b?.backend==="feishu"?"原始消息编号 om_…":"原始工单或 PR 编号";
const submit=node("button","只读核对");
submit.type="submit";
submit.disabled=busy;
form.append(input,submit);
form.addEventListener("submit",e=>{e.preventDefault();
const key=b?.backend==="github"?"pull_number":b?.backend==="github_review"?"issue_number":"message_id";
let value=input.value.trim();
if(key!=="message_id"){if(!/^[1-9][0-9]{0,9}$/.test(value)){notice("请填写正整数编号。");
return;
}value=Number(value);
}runAction(row,"reconcile",{[key]:value});
});
pane.append(form);
}
    const actions=node("div",undefined,"actions");
if(row.state==="completed"&&i.operation==="deliver"&&identity.permissions.includes("business:propose")){const undo=node("button","创建补偿草稿");
undo.disabled=busy;
undo.addEventListener("click",()=>proposeUndo(row));
actions.append(undo);
}if(row.state==="pending"){if(identity.permissions.includes("actions:resume")){const execute=node("button","执行已批准动作","primary");
execute.disabled=busy;
execute.addEventListener("click",()=>runAction(row,"execute",{}));
actions.append(execute);
}if(identity.permissions.includes("business:propose")){const cancel=node("button","取消草稿");
cancel.disabled=busy;
cancel.addEventListener("click",()=>runAction(row,"cancel",{}));
actions.append(cancel);
}}pane.append(actions);

  }
  async function load(append=false){const page=await api(`drafts?limit=25${append&&cursor!==null?`&before=${cursor}`:""}`);
rows=append?[...rows,...page.items]:page.items;
cursor=page.next_cursor;
drawList();
if(selected){const row=rows.find(r=>r.intent.action_id===selected);
if(row)drawDetail(row);
else{selected=null;
byId("detail").replaceChildren(node("p","该行动未在当前列表，请重新选择。"));
}}}
  async function proposeUndo(row){
    if(busy)return;
busy=true;
notice("正在创建补偿草稿，后续仍需单独审批。");

    const i=row.intent,now=new Date();

    const draft={action_id:crypto.randomUUID(),task_id:i.task_id,thread_id:i.thread_id,
      idempotency_key:`undo:${i.action_id}`,destination:i.destination,expected_revision:row.receipt.revision,
      payload:i.payload,operation:"compensate",compensates:i.action_id,created_at:now.toISOString(),
      expires_at:new Date(now.getTime()+3600000).toISOString()};

    try{const result=await api("drafts",draft);
selected=result.intent.action_id;
await load();
notice("补偿草稿已创建，请完成独立审批后执行。");
}
    catch(e){notice(e.message);
}finally{busy=false;
const current=rows.find(r=>r.intent.action_id===selected);
if(current&&identity)drawDetail(current);
}
  }
  async function runAction(row,action,body){if(busy)return;
busy=true;
notice("正在处理，请勿重复操作。");
drawDetail(row);
try{await api(`drafts/${row.intent.action_id}/${action}`,body);
await load();
notice(action==="reconcile"?"已完成只读核对。":action==="cancel"?"草稿已取消。":"执行完成，请查看交付凭据。");
}catch(e){notice(e.message);
}finally{busy=false;
const current=rows.find(r=>r.intent.action_id===selected);
if(current&&identity)drawDetail(current);
}}
  byId("connect-form").addEventListener("submit",async e=>{e.preventDefault();
if(busy)return;
busy=true;
const value=byId("credential").value;
notice("正在连接…");
try{const response=await fetch("/business/session",{headers:{authorization:`Bearer ${value}`},cache:"no-store",credentials:"omit",redirect:"error"});
if(!response.ok)throw new Error("凭证无效、已过期或没有查看权限。");
const next=await response.json();
if(!next.data_domains.length)throw new Error("此账号没有可查看的数据范围。");
generation++;
token=value;
identity=next;
byId("credential").value="";
byId("scope").textContent=`${identity.user_id} / ${identity.project_id} · ${identity.data_domains.join("、")}`;
byId("connection").hidden=true;
byId("workspace").hidden=false;
byId("logout").hidden=false;
await load();
notice("");
}catch(err){disconnect();
notice(err.message);
}finally{busy=false;
}});

  byId("logout").addEventListener("click",disconnect);

  byId("refresh").addEventListener("click",()=>load().then(()=>notice("记录已刷新。")).catch(e=>notice(e.message)));

  byId("more").addEventListener("click",()=>load(true).catch(e=>notice(e.message)));

})();

