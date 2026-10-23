"use strict";
const $ = (id) => document.getElementById(id);
const groups = {access:"基础与访问",models:"模型与账号",execution:"执行与用量",conversation:"对话与任务",notifications:"通知与事件",resources:"记忆与文件",connections:"外部连接",extensions:"扩展插件"};
let snapshot, inventory, resourceInventory=[], selected="access", expandedOwner;
const resourceEdits=new Map();
const dirty=()=>edits.size+resourceEdits.size;
const edits=new Map();
const invalid=new Map();
const drafts=new Map();
const hiddenValue=(value)=>value==="••••••••"||(value&&typeof value==="object"&&(Object.keys(value).length===1&&Object.hasOwn(value,"configured")||Object.values(value).some(hiddenValue)));
const key=(path)=>JSON.stringify(path);
const node=(tag,text,cls)=>{const e=document.createElement(tag);if(text!==undefined)e.textContent=text;if(cls)e.className=cls;return e;};
async function api(path, body) {
  const response=await fetch(path,body===undefined?{}:{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  if(response.status===401){$("login").hidden=false;$("editor").hidden=true;throw Error("请先登录");}
  if(!response.headers.get("Content-Type")?.includes("application/json"))throw Error(`请求失败（${response.status}）`);
  const data=await response.json();if(!response.ok)throw Error(data.error||"请求失败");return data;
}
async function action(fn){try{await fn();}catch(error){$("notice").textContent=error.message;}}
function changed(path,op,value){
  invalid.delete(key(path));if(op==="default"||op==="clear_secret")drafts.delete(key(path));
  const parent=[...edits.values()].find(e=>e.op==="set"&&e.value&&typeof e.value==="object"&&e.path.length<path.length&&e.path.every((p,i)=>p===path[i]));
  if(parent){
    let target=parent.value;for(const part of path.slice(parent.path.length,-1))target=target[part]??={};if(op==="default"||op==="clear_secret")delete target[path.at(-1)];else target[path.at(-1)]=value;
  }else{
    for(const [id,e] of edits)if(path.length<e.path.length&&path.every((p,i)=>p===e.path[i]))edits.delete(id);
    edits.set(key(path),{path,op,...(value===undefined?{}:{value})});
  }
  $("count").textContent=`${dirty()} 项未保存`;
}
function drawField(field){
  if(typeof field.schema!=="object")field={...field,schema:field.schema===false?{readOnly:true,description:"插件规范不允许此字段"}:{}};
  const rule=field.schema,path=field.path,id=key(path),edit=edits.get(id),secret=rule.writeOnly||rule["x-sensitive"];
  let value=edit&&Object.hasOwn(edit,"value")?edit.value:field.value;
  if(edit?.op==="default")value=rule.default;
  if(value===undefined)value=rule.default;
  const row=node("section",undefined,"field"),heading=node("div",undefined,"field-heading");
  const label=node("label",rule.title||path.at(-1));label.htmlFor=id;
  heading.append(label,node("span",field.explicit?"已配置":"默认值","badge"));row.append(heading);
  row.append(node("code",path.join("."),"path"));
  if(rule["x-readonly-reason"])row.append(node("p",rule["x-readonly-reason"],"hint"));
  if(Object.hasOwn(field,"loaded_value") && JSON.stringify(field.loaded_value)!==JSON.stringify(field.value) && !secret)row.append(node("p","已加载值："+JSON.stringify(field.loaded_value),"hint"));
  if(rule.description)row.append(node("p",rule.description,"hint"));
  if(rule.type==="object" && (rule.properties||Object.keys(rule.additionalProperties||{}).length) && !secret){
    const nested=node("details");nested.open=true;nested.append(node("summary","配置字段"));
    const current=structuredClone(value||{});
    for(const operation of edits.values())if(operation.path.length>path.length && path.every((p,i)=>operation.path[i]===p)){
      let target=current;const trail=operation.path.slice(path.length);for(const part of trail.slice(0,-1))target=target[part]??={};
      if(operation.op==="set")target[trail.at(-1)]=operation.value;
    }
    const properties=displayProperties(rule,current);
    for(const name of Object.keys(current))if(!properties[name])properties[name]=rule.additionalProperties||{};
    for(const [name,child] of Object.entries(properties))nested.append(drawField({...field,path:[...path,name],schema:{...child,readOnly:rule.readOnly||child.readOnly,"x-removable":!Object.hasOwn(rule.properties||{},name)},value:current[name],loaded_value:field.loaded_value?.[name],explicit:field.explicit&&Object.hasOwn(current,name)}));
    if(rule.additionalProperties && !rule.readOnly){
      const add=node("button","添加条目","subtle");add.type="button";
      add.addEventListener("click",()=>{const name=prompt("条目名称（稳定标识）");if(!name)return;if(!/^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(name)||Object.hasOwn(current,name)){ $("notice").textContent="名称不合法或已经存在";return;}changed([...path,name],"set",{});render();});nested.append(add);
    }
    row.append(nested);
    if(!rule.readOnly){const reset=node("button",rule["x-removable"]?"移除此条目":"恢复此组默认值","subtle");reset.addEventListener("click",()=>{if(!confirm("移除此配置覆盖？不会删除业务数据，保存及重启后生效。"))return;changed(path,"default");render();});row.append(reset);}
    return row;
  }
  let input,read;
  if(path.length===1&&path[0]==="plugins"){
    input=node("div",undefined,"plugin-picker");
    const available=new Map(inventory.plugins.map(plugin=>[plugin.id,plugin]));
    for(const name of value||[])if(!available.has(name))available.set(name,{id:name,kind:"unavailable"});
    for(const plugin of available.values()){
      const line=node("label",undefined,"plugin-option"),check=node("input");check.type="checkbox";check.value=plugin.id;check.checked=(value||[]).includes(plugin.id);
      line.append(check,node("span",plugin.configuration?.title||plugin.id),node("code",plugin.id));
      if(plugin.kind==="unavailable")line.append(node("span","未安装","badge"));input.append(line);
    }
    read=()=>[...input.querySelectorAll("input:checked")].map(i=>i.value);
  }
  else if(secret){input=node("input");input.type="password";input.autocomplete="new-password";input.placeholder=value?.configured?"已配置 · 留空保持":"未配置";read=()=>input.value;}
  else if(rule["x-reference"]){input=node("select");const values=referenceValues(rule["x-reference"]);for(const v of new Set(["",...values,...(value?[value]:[])])){const option=node("option",String(v)||"未指定");option.value=String(v);input.append(option);}input.value=String(value??"");read=()=>input.value;}
  else if(rule.enum){input=node("select");for(const v of rule.enum){const o=node("option",String(v));o.value=String(v);input.append(o);}input.value=String(value??"");read=()=>rule.enum.find(v=>String(v)===input.value);}
  else if(rule.type==="boolean"){input=node("input");input.type="checkbox";input.checked=Boolean(value);read=()=>input.checked;}
  else if(rule.type==="number"||rule.type==="integer"){input=node("input");input.type="number";input.step=rule.type==="integer"?"1":"any";if(rule.minimum!==undefined)input.min=rule.minimum;if(rule.maximum!==undefined)input.max=rule.maximum;input.value=value??"";read=()=>{if(!input.value||!input.checkValidity())throw Error("请填写符合限制的数值");return Number(input.value);};}
  else if(!rule.type||rule.type==="object"||rule.type==="array"){input=node("textarea");input.spellcheck=false;input.value=JSON.stringify(value??(rule.type==="array"?[]:{}),null,2);read=()=>JSON.parse(input.value);}
  else {input=node("input");input.type="text";input.value=value??"";read=()=>input.value;}
  if(drafts.has(id)&&input.tagName!=="DIV"&&input.type!=="checkbox")input.value=drafts.get(id);
  input.id=id;input.disabled=Boolean(rule.readOnly||(!secret&&hiddenValue(value)));input.addEventListener("input",()=>{
    if(input.tagName!=="DIV")drafts.set(id,input.value);
    try{changed(path,secret?"replace_secret":"set",read());input.setAttribute("aria-invalid","false");if(input.tagName==="SELECT"||input.type==="checkbox")render();}
    catch{invalid.set(id,path.join("."));input.setAttribute("aria-invalid","true");$("notice").textContent="字段格式不正确："+path.join(".");}
  });row.append(input);
  const details=node("div",undefined,"field-bottom");details.append(node("span",field.files.length?`文件：${field.files.join("、")}`:`尚未写入 · ${snapshot.entry}`,"hint"));
  if(!rule.readOnly){const reset=node("button",secret?"清除凭据":"恢复默认","subtle");reset.type="button";reset.addEventListener("click",()=>{if(secret&&!confirm("清除此凭据？保存并重启后生效。"))return;changed(path,secret?"clear_secret":"default");render();});details.append(reset);}
  else details.append(node("span","仅本机维护","badge"));row.append(details);return row;
}
function render(){
  $("documents").replaceChildren();$("navigation").replaceChildren();
  const available=[...new Set([...snapshot.fields.map(f=>f.group),...resourceInventory.filter(r=>r.kind!=="asset").map(resourceGroup)])];if(!available.includes(selected))selected=available[0];
  for(const group of available){const button=node("button",groups[group]||group,group===selected?"active":"subtle");button.addEventListener("click",()=>{selected=group;render();});$("navigation").append(button);}
  const fields=snapshot.fields.filter(f=>f.group===selected);
  const owners=[...new Set(fields.map(f=>f.owner))];
  const enabled=edits.get(key(["plugins"]))?.value??snapshot.fields.find(f=>key(f.path)===key(["plugins"]))?.value??[];
  const extension=owner=>inventory.plugins.find(p=>p.configuration?.title===owner);
  if(!owners.includes(expandedOwner))expandedOwner=owners.find(owner=>enabled.includes(extension(owner)?.id))||owners[0];
  for(const owner of owners){
    const plugin=extension(owner),collapsible=selected==="extensions";
    const section=node(collapsible?"details":"article",undefined,collapsible?"owner-panel":undefined),advanced=node("details");
    if(collapsible){
      section.open=owner===expandedOwner;
      section.append(node("summary",owner+" · "+(enabled.includes(plugin?.id)?"已启用":"未启用")));
      section.addEventListener("toggle",()=>{if(section.open){expandedOwner=owner;for(const other of $("documents").querySelectorAll(".owner-panel"))if(other!==section)other.open=false;}});
      if(!enabled.includes(plugin?.id))section.append(node("p","此插件尚未启用；保存配置不会自动启用它。请在基础与访问中选择插件，保存时会校验完整依赖。","hint"));
    }else section.append(node("h2",owner));
    advanced.append(node("summary","高级设置"));
    for(const field of fields.filter(f=>f.owner===owner))(field.schema["x-advanced"]?advanced:section).append(drawField(field));
    if(advanced.childElementCount>1)section.append(advanced);$("documents").append(section);
  }
  drawResources();
  if(selected==="models")action(drawAccounts);
  $("count").textContent=`${dirty()} 项未保存`;
}
async function load(){
  if((dirty()||invalid.size)&&!confirm("重新加载会放弃尚未保存的编辑，是否继续？"))return;
  const [config,status,descriptions,resources]=await Promise.all([api("/api/configuration"),api("/api/status"),api("/api/catalog"),api("/api/resources")]);
  resourceInventory=resources.resources;resourceEdits.clear();
  snapshot=config;inventory=descriptions;edits.clear();invalid.clear();drafts.clear();
  $("diagnostics").replaceChildren(...(inventory.issues||[]).map(item=>node("p",`${item.id}：配置描述不可用（${item.error}）`,"hint")));$("login").hidden=true;$("editor").hidden=false;
  $("state").textContent=status.loaded_version===config.version?"文件与已加载版本一致":"已保存的配置待重启生效";
  $("health").textContent=`业务状态：${({running:"运行中",stopped:"已停止",failed:"启动失败",degraded:"部分功能不可用",starting:"启动中"})[status.state]||status.state}`;
  render();await applicationStatus();
}
$("login").addEventListener("submit",e=>{e.preventDefault();action(async()=>{await api("/api/login",{token:$("token").value});$("token").value="";await load();$("notice").textContent="";});});
$("refresh").addEventListener("click",()=>action(load));
$("save").addEventListener("click",()=>action(async()=>{
  if(invalid.size)throw Error("请先修正字段格式："+[...invalid.values()].join("、"));
  if(!dirty()){$("notice").textContent="没有未保存的修改。";return;}
  $("save").disabled=true;
  try{const saved=await api("/api/configuration",{version:snapshot.version,patches:[...edits.values()],resources:[...resourceEdits.values()]});edits.clear();resourceEdits.clear();await load();$("notice").textContent=saved.changed?"已保存，待重启生效。":"没有配置变更。";}finally{$("save").disabled=false;}
}));
$("history").addEventListener("click",()=>action(async()=>{const history=await api("/api/history");$("versions").replaceChildren(node("h2","变更记录"));for(const item of history.versions){const article=node("article"),restore=node("button","恢复此版本","subtle");article.append(node("p",new Date(item.time*1000).toLocaleString()+" · "+item.fields.join("、")));restore.addEventListener("click",()=>action(async()=>{if(dirty())throw Error("请先保存或放弃当前编辑");if(!confirm("恢复此前的配置？业务数据不会回滚，保存后仍需重启。"))return;await api("/api/restore",{id:item.id,version:snapshot.version});await load();}));article.append(restore);$("versions").append(article);}}));
window.addEventListener("beforeunload",e=>{if(dirty()||invalid.size){e.preventDefault();e.returnValue="";}});
action(load);

let applicationTimer;
async function applicationStatus(){
  const status=await api("/api/status"),application=status.application||{phase:"idle"};
  const labels={idle:"尚未发起应用",preparing:"校验配置中",waiting:"正在等待工作结束",cancelling:"正在取消在途工作",restarting:"正在重启业务服务",applied:"已应用指定配置版本",partially_applied:"已应用，但部分功能或外部部署步骤尚未就绪",failed:"应用失败，请查看业务诊断",withdrawn:"已撤销等待，继续接收新工作"};
  $("application-state").textContent=labels[application.phase]||application.phase;
  if(application.phase==="applied"&&snapshot&&status.loaded_version===snapshot.version)$("state").textContent="文件与已加载版本一致（重新加载可更新字段详情）";
  const workLabels={queued:"等待执行",dispatching:"正在启动",running:"执行中",waiting_user:"等待你回答",cancelling:"正在取消",unknown:"结果待核实",paused:"已暂停，需要处理",scheduled_immediate:"等待派发",finalizing:"正在保存结果",model_review:"正在整理执行评估",model_call:"模型请求尚未结束",memory_capture:"正在处理记忆",capability_call:"操作尚未结束",drain_not_supported:"插件尚不支持等待检查；可撤销等待或选择终止并重启"};
  $("blockers").replaceChildren(...(application.blockers||[]).map(item=>{
    const owner=inventory?.plugins.find(plugin=>plugin.id===item.owner)?.configuration?.title||item.owner||"";
    const message=item.owner==="admission"?"正在准备已接受的请求":(workLabels[item.state]||"操作尚未结束");
    const row=node("li",`${owner} ${item.id?item.id.slice(0,12):""} · ${message}`);if(item.id)row.title=item.id;return row;
  }),...(application.required_steps||[]).map(item=>node("li",item.message)));
  for(const step of application.required_steps||[])if(application.phase==="partially_applied"){
    const li=node("li"),button=node("button","确认已手动完成 "+step.component,"subtle");button.addEventListener("click",()=>action(async()=>{if(!confirm("仅在已完成并验证此外部部署步骤后确认；此按钮不会执行部署。"))return;await api("/api/complete-step",{version:application.version,component:step.component,confirmed:true});await applicationStatus();}));li.append(button);$("blockers").append(li);
  }
  if(application.management_address){const address=application.management_address;let host=["0.0.0.0","::"].includes(address.host)?location.hostname:address.host;if(host.includes(":")&&!host.startsWith("["))host=`[${host}]`;const target=address.public_origin||`${location.protocol}//${host}:${address.port}`;if(target!==location.origin){const link=node("a","打开新的配置地址");link.href=target;$("application-state").append(" · ",link);}}
  if(application.resume_failed)$("application-state").append("；恢复接收新工作失败，请检查插件或终止并重启。");
  const active=["preparing","waiting","cancelling","restarting"].includes(application.phase);
  $("apply-wait").disabled=active;$("apply-force").disabled=active;$("abort-apply").disabled=!["preparing","waiting"].includes(application.phase);
  if(active&&!applicationTimer)applicationTimer=setInterval(()=>action(applicationStatus),1000);
  if(!active&&applicationTimer){clearInterval(applicationTimer);applicationTimer=undefined;}
}
async function applyConfiguration(mode){
  if(dirty()||invalid.size)throw Error("请先保存或放弃尚未保存的编辑");
  const preview=await api("/api/application-preview");
  if(!confirm(`将应用版本 ${preview.version.slice(0,12)}，并重启业务服务。\n变更字段：${preview.fields.join("、")||"无文件变化"}\n${(preview.required_steps||[]).map(step=>step.message).join("\n")}\n${mode==="wait"?"等待已接受的工作完成；等待期间可撤销。":"当前工作将被终止，外部操作可能已经发生。"}`))return;
  if(mode==="force"&&!confirm("再次确认：取消在途工作并终止业务进程？未知结果不会自动重做。"))return;
  await api("/api/apply",{version:preview.version,mode,confirmed:true});await applicationStatus();
}
$("apply-wait").addEventListener("click",()=>action(()=>applyConfiguration("wait")));
$("apply-force").addEventListener("click",()=>action(()=>applyConfiguration("force")));
$("abort-apply").addEventListener("click",()=>action(async()=>{await api("/api/abort-apply",{});await applicationStatus();}));

function matches(value, condition){
  if(!condition)return true;
  if(condition.required?.some(name=>!Object.hasOwn(value||{},name)))return false;
  if(condition.const!==undefined && JSON.stringify(condition.const)!==JSON.stringify(value))return false;
  if(condition.enum && !condition.enum.some(item=>JSON.stringify(item)===JSON.stringify(value)))return false;
  if(condition.properties && Object.entries(condition.properties).some(([name,rule])=>Object.hasOwn(value||{},name)&&!matches(value[name],rule)))return false;
  return (!condition.allOf||condition.allOf.every(c=>matches(value,c)))&&(!condition.anyOf||condition.anyOf.some(c=>matches(value,c)));
}
function referenceValues(path){
  if(typeof path!=="string")return [];
  const parts=path.split(".");
  const field=snapshot.fields.find(field=>key(field.path)===key(parts));
  const current=edits.get(key(parts))?.value??field?.value;
  return current&&typeof current==="object"?Object.keys(current):[];
}
function resourceGroup(resource){return ({credentials:"models",mcp:"connections",management:"access",web_access:"access",gateway_key:"models"})[resource.id]||(resource.kind==="skill"?"resources":"extensions");}
function drawResources(){
  const resources=resourceInventory.filter(r=>r.kind!=="asset"&&resourceGroup(r)===selected);
  if(!resources.length)return;
  const section=node("article");section.append(node("h2","配置文件与凭据"));
  for(const resource of resources){
    const row=node("details");row.append(node("summary",resource.title+" · "+(resource.configured?"已配置":"未配置")),node("p",resource.file,"hint"));
    if(resource.read_only){row.append(node("p","此路径在实例之外，需要本机维护；网页不会读取或修改它。","hint"));section.append(row);continue;}
    if(resource.kind==="dotenv"){
      const names=new Set((resource.keys||[]).map(k=>k.name));
      const add=node("button","添加私有凭据","subtle");add.addEventListener("click",()=>{const name=prompt("环境变量名称，例如 DEEPSEEK_API_KEY");if(name&&/^[A-Z_][A-Z0-9_]*$/.test(name)&&!names.has(name)){resource.keys.push({name,configured:false,source:"file"});render();}});row.append(add);
      for(const entry of resource.keys||[]){
        const label=node("label",entry.name+" · "+(entry.source==="environment"?(entry.environment_configured?"环境变量覆盖文件":"环境变量为空，请在服务环境中维护"):entry.configured?"文件中已配置":"未配置"));
        const input=node("input");input.type="password";input.autocomplete="new-password";input.placeholder="留空保持";
        input.disabled=entry.source==="environment";input.value=resourceEdits.get(resource.id+":"+entry.name)?.value??"";
        input.addEventListener("input",()=>{const id=resource.id+":"+entry.name;if(input.value)resourceEdits.set(id,{id:resource.id,key:entry.name,op:"replace_secret",value:input.value});else resourceEdits.delete(id);$("count").textContent=`${dirty()} 项未保存`;});
        const clear=node("button","清除","subtle");clear.disabled=input.disabled;clear.addEventListener("click",()=>{if(confirm("清除此文件凭据？保存及重启后生效。")){resourceEdits.set(resource.id+":"+entry.name,{id:resource.id,key:entry.name,op:"clear_secret"});input.value="";$("count").textContent=`${dirty()} 项未保存`;}});label.append(input,clear);row.append(label);
      }
    } else {
      const input=node(resource.sensitive?"input":"textarea");if(resource.sensitive){input.type="password";input.autocomplete="new-password";input.placeholder="已有值不回显；输入新值替换，留空保持";input.value=resourceEdits.get(resource.id)?.value??"";}else input.value=resourceEdits.get(resource.id)?.value??resource.value??"";
      input.addEventListener("input",()=>{resourceEdits.set(resource.id,{id:resource.id,op:resource.sensitive?"replace_secret":"set",value:input.value});$("count").textContent=`${dirty()} 项未保存`;});row.append(input);
      if(resource.id==="management")row.append(node("p","修改管理地址或生命周期后，应用时会切换管理监听地址。请记录新地址，浏览器连接可能中断；绑定失败会保留旧入口并明确报告。","hint"));
      if(resource.id==="mcp")row.append(node("p","提交 MCP 定义 JSON；为防泄露，已有注册表不回显。版本由所属模块递增。认证建议引用上面的环境凭据。","hint"));
    }
    section.append(row);
  }
  $("documents").append(section);
}
let accountTimer;
async function drawAccounts(){
  const data=await api("/api/accounts");if(selected!=="models")return;
  let section=$("accounts-panel");if(!section){section=node("article");section.id="accounts-panel";$("documents").append(section);}section.replaceChildren(node("h2","原生账号授权"),node("p","账号操作即时生效，复用 Pi 授权；配置恢复不会撤销登录、退出或令牌轮换。","hint"));
  for(const account of data.accounts){const row=node("section",undefined,"field");row.append(node("strong",`${account.name} · ${account.provider} · ${account.auth}`));
    for(const [name,label] of [["status","检查授权"],["catalog","可用模型"],["login","登录"],["logout","退出授权"]]){
      if(account.environment_key&&["login","logout"].includes(name))continue;
      const button=node("button",label,"subtle");button.addEventListener("click",()=>action(async()=>{if(["login","logout"].includes(name)&&!confirm(`${label}会立即改变账号授权，影响正在使用此账号的模型。是否继续？`))return;const result=await api("/api/account",{name:account.name,action:name,confirmed:true});const output=node("pre",JSON.stringify(result,null,2));row.append(output);await pollAccounts();}));row.append(button);
    }section.append(row);
  }
  drawFlows(data.flows,section);
}
function drawFlows(flows,section){
  for(const flow of flows){const row=node("section",undefined,"field");row.append(node("p",`${flow.name} · ${flow.state}`));if(flow.message)row.append(node("p",flow.message));
    if(flow.authorization_url){const link=node("a","打开官方授权页面");link.href=flow.authorization_url;link.target="_blank";link.rel="noopener noreferrer";row.append(link,node("p","回调若为 localhost，请将授权端口转发至运行服务的机器。","hint"));}
    if(flow.prompt){const label=node("label",flow.prompt.message),input=node("input");input.type="password";input.autocomplete="off";const answer=node("button","提交授权信息");answer.addEventListener("click",()=>action(async()=>{await api("/api/account-answer",{id:flow.id,prompt_id:flow.prompt.id,value:input.value});input.value="";await pollAccounts();}));label.append(input,answer);row.append(label);}
    if(flow.state==="pending"){const cancel=node("button","取消授权","subtle");cancel.addEventListener("click",()=>action(async()=>{await api("/api/account-cancel",{id:flow.id});await pollAccounts();}));row.append(cancel);}section.append(row);
  }
}
async function pollAccounts(){
  clearTimeout(accountTimer);if(selected!=="models")return;
  const data=await api("/api/accounts");let section=$("account-flows");if(!section){section=node("article");section.id="account-flows";$("documents").append(section);}section.replaceChildren();drawFlows(data.flows,section);
  if(data.flows.some(f=>f.state==="pending"))accountTimer=setTimeout(()=>action(pollAccounts),1500);
}
$("repair-open").addEventListener("click",()=>action(async()=>{
  const data=await api("/api/repair");const panel=$("repair");panel.hidden=false;panel.replaceChildren(node("h2","修复已登记配置文件"),node("p","只替换下面列出的文件。损坏内容可能含有密钥，因此不会回显；请使用本机内容或备份填写完整 TOML。保存前仍校验整个配置。","hint"));
  const select=node("select");for(const name of data.files){const option=node("option",name);option.value=name;select.append(option);}const text=node("textarea"),save=node("button","校验并修复");text.spellcheck=false;save.addEventListener("click",()=>action(async()=>{if(!confirm("替换此配置文件？不会重启业务或回滚业务数据。"))return;await api("/api/repair",{version:data.version,files:{[select.value]:text.value},confirmed:true});panel.hidden=true;await load();$("notice").textContent="已修复，待重启生效。";}));panel.append(select,text,save);
}));

function displayProperties(rule,current){
  let props={...rule.properties};
  for(const branch of rule.allOf||[])Object.assign(props,displayProperties(branch,current));
  if(rule.if)Object.assign(props,displayProperties((matches(current,rule.if)?rule.then:rule.else)||{},current));
  return props;
}
