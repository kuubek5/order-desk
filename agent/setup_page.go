package main

// setupHTML is the whole settings menu, served at http://127.0.0.1:8766/.
// Self-contained (no external assets) so it works on an isolated machine PC.
const setupHTML = `<!doctype html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KMill Agent — налаштування</title>
<style>
  :root{
    --bg:#12100e; --panel:#1c1915; --line:#332c24; --ink:#f3ede4;
    --ink2:#b9ad9c; --amber:#f0a24b; --amber2:#f6c07a; --ok:#54c98a; --bad:#e8695f;
    --mono:ui-monospace,"Cascadia Mono",Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:#12100e;color:#f3ede4;
    font:15px/1.5 "Segoe UI",system-ui,sans-serif;padding:28px 18px}
  .wrap{max-width:620px;margin:0 auto}
  h1{font-size:22px;margin:0 0 4px;font-weight:800}
  h1 span{color:#f0a24b}
  .sub{color:#b9ad9c;margin:0 0 22px;font-size:13px}
  .card{background:#1c1915;border:1px solid #332c24;border-radius:14px;
    padding:18px 18px 20px;margin-bottom:16px}
  .card h2{font-size:13px;text-transform:uppercase;letter-spacing:.06em;
    color:#f6c07a;margin:0 0 14px;font-weight:700}
  label{display:block;font-size:12px;color:#b9ad9c;margin:0 0 5px}
  .row{margin-bottom:14px}
  input,select,option{width:100%;background:#241c11;border:1px solid #6b5836;
    color:#f3ede4;border-radius:9px;padding:10px 12px;font-size:14px}
  option{background:#241c11;color:#f3ede4}
  input.mono{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;letter-spacing:.02em}
  .inline{display:flex;gap:8px}
  .inline input{flex:1}
  button{font:inherit;font-weight:600;border:0;border-radius:9px;padding:10px 16px;
    cursor:pointer;background:#2a231b;color:#f3ede4}
  button:hover{background:#332a20}
  button.primary{background:#f0a24b;color:#1a1206}
  button.primary:hover{background:#f6c07a}
  button.ghost{background:transparent;border:1px solid #332c24;color:#b9ad9c}
  .kv{display:flex;justify-content:space-between;gap:12px;padding:7px 0;
    border-bottom:1px dashed #332c24;font-size:13px}
  .kv:last-child{border-bottom:0}
  .kv b{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;color:#f3ede4;font-weight:600;text-align:right;
    word-break:break-all}
  .crm{display:flex;gap:8px;align-items:center;margin:6px 0}
  .crm code{flex:1;font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:13px;background:#0d0b09;
    border:1px solid #332c24;border-radius:8px;padding:9px 11px;word-break:break-all}
  .pill{display:inline-block;font-size:12px;padding:2px 9px;border-radius:999px;font-weight:600}
  .pill.ok{background:rgba(84,201,138,.15);color:#54c98a}
  .pill.bad{background:rgba(232,105,95,.15);color:#e8695f}
  .hint{font-size:12px;color:#b9ad9c;margin-top:6px}
  img#prev{width:100%;border:1px solid #332c24;border-radius:10px;margin-top:10px;
    background:#0d0b09;min-height:80px}
  .save{display:flex;gap:10px;align-items:center;margin-top:6px}
  #msg{font-size:13px}
  #msg.ok{color:#54c98a} #msg.bad{color:#e8695f}
  .warns{color:#f6c07a;font-size:12px;margin-top:8px;white-space:pre-line}
</style>
</head>
<body>
<div class="wrap">
  <h1><span>KMill</span> Agent — налаштування</h1>
  <p class="sub">Заповни поля, натисни «Зберегти й запустити». Далі перенеси адресу
    й токен у KMill → Налаштування → Верстати.</p>

  <div class="card">
    <h2>Налаштування верстата</h2>
    <div class="row">
      <label>Назва верстата (для зручності)</label>
      <input id="name" placeholder="напр. 350i">
    </div>
    <div class="row">
      <label>Токен (спільний секрет із CRM)</label>
      <div class="inline">
        <input id="token" class="mono">
        <button class="ghost" onclick="gen()">Новий</button>
        <button class="ghost" onclick="copyEl('token')">Копіювати</button>
      </div>
      <div class="hint">Той самий рядок вписується в KMill. «Новий» = випадковий токен.</div>
    </div>
    <div class="inline">
      <div class="row" style="flex:1">
        <label>Монітор (де RemiCORE)</label>
        <select id="display"></select>
      </div>
      <div class="row" style="width:120px">
        <label>Порт</label>
        <input id="port" class="mono" value="8765">
      </div>
    </div>
    <div class="save">
      <button class="primary" onclick="save()">Зберегти й запустити</button>
      <span id="msg"></span>
    </div>
    <div id="warns" class="warns"></div>
  </div>

  <div class="card">
    <h2>Впиши це в KMill</h2>
    <div id="crmlist"></div>
    <div class="hint">Адреса агента для поля «адреса верстата» в CRM.</div>
  </div>

  <div class="card">
    <h2>Стан</h2>
    <div class="kv"><span>Ім'я ПК</span><b id="host">—</b></div>
    <div class="kv"><span>Автозапуск</span><b id="task">—</b></div>
    <div class="kv"><span>Підніметься після ребуту</span><b id="boot">—</b></div>
    <div class="kv"><span>Права адміністратора</span><b id="admin">—</b></div>
    <div class="kv"><span>Версія агента</span><b id="ver">—</b></div>
  </div>

  <div class="card">
    <h2>Що бачитиме CRM</h2>
    <button class="ghost" onclick="prev()">Оновити кадр</button>
    <img id="prev" alt="натисни «Оновити кадр»">
  </div>

  <div style="text-align:center;margin:8px 0 24px">
    <button class="ghost" onclick="quit()">Готово — закрити</button>
  </div>
</div>

<script>
function el(id){return document.getElementById(id);}

function genToken(){
  var hex="0123456789abcdef",out="",i,r,arr=null;
  try{ if(window.crypto&&crypto.getRandomValues){arr=new Uint8Array(16);crypto.getRandomValues(arr);} }catch(e){arr=null;}
  for(i=0;i<16;i++){ r=arr?arr[i]:Math.floor(Math.random()*256); out+=hex.charAt((r>>4)&15)+hex.charAt(r&15); }
  return out;
}
function gen(){ el("token").value=genToken(); }

function copyText(text){
  try{ if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(text);return;} }catch(e){}
  var ta=document.createElement("textarea");ta.value=text;ta.style.position="fixed";ta.style.opacity="0";
  document.body.appendChild(ta);ta.focus();ta.select();
  try{ document.execCommand("copy"); }catch(e){}
  document.body.removeChild(ta);
}
function copyEl(id){ copyText(el(id).value); }

function xhr(method,url,body,onok){
  var r=new XMLHttpRequest();
  r.open(method,url,true);
  if(body!==null&&body!==undefined){ r.setRequestHeader("Content-Type","application/json"); }
  r.onreadystatechange=function(){
    if(r.readyState!==4) return;
    var data=null; try{ data=JSON.parse(r.responseText); }catch(e){ data=null; }
    onok((r.status>=200&&r.status<300)?data:null);
  };
  r.send((body!==undefined&&body!==null)?body:null);
}

function load(){
  xhr("GET","/info",null,function(d){
    if(!d) return;
    el("name").value=d.name||"";
    el("token").value=d.token||"";
    el("port").value=d.port||"8765";
    var sel=el("display");sel.innerHTML="";
    var n=d.displays||1,i;
    for(i=0;i<n;i++){
      var o=document.createElement("option");
      o.value=i;o.text="Монітор "+i+(i===0?" (головний)":"");
      if(i===d.display) o.selected=true;
      sel.appendChild(o);
    }
    el("host").textContent=d.hostname||"—";
    el("admin").innerHTML=d.admin?'<span class="pill ok">так</span>':'<span class="pill bad">ні — запусти від адміністратора</span>';
    el("task").innerHTML=d.taskInstalled?(d.taskRunning?'<span class="pill ok">працює</span>':'<span class="pill bad">зупинено</span>'):'<span class="pill bad">не встановлено</span>';
    // «Задача є» — ще НЕ «підніметься». Задача, привʼязана до одного акаунта,
    // виглядає здоровою, але після ребуту не спрацьовує для оператора. Тому
    // окремий рядок: зелений лише коли є групова задача АБО спільний ярлик.
    var ok=d.taskAnyUser||d.startupLnk||d.runKey, why=[];
    if(d.taskAnyUser) why.push("задача для всіх користувачів");
    else if(d.taskOwner) why.push("задача лише для "+d.taskOwner);
    if(d.startupLnk) why.push("ярлик автозавантаження");
    if(d.runKey) why.push("ключ реєстру");
    el("boot").innerHTML=(ok?'<span class="pill ok">так</span>':'<span class="pill bad">НІ — перевстанови агента</span>')
      +(why.length?' <small>'+why.join(", ")+'</small>':'');
    el("ver").textContent=d.version||"—";
    var ips=d.ips||[],box=el("crmlist");box.innerHTML="";
    if(!ips.length){ box.innerHTML='<div class="hint">IP не знайдено — перевір мережу.</div>'; }
    for(i=0;i<ips.length;i++){
      (function(ip){
        var url="http://"+ip+":"+(d.port||"8765");
        var div=document.createElement("div");div.className="crm";
        var code=document.createElement("code");code.textContent=url;
        var b=document.createElement("button");b.className="ghost";b.textContent="Копіювати";
        b.onclick=function(){ copyText(url); };
        div.appendChild(code);div.appendChild(b);box.appendChild(div);
      })(ips[i]);
    }
  });
}
function prev(){ el("prev").src="/preview?display="+el("display").value+"&t="+(new Date().getTime()); }
function quit(){
  xhr("POST","/quit",null,function(){
    document.body.innerHTML='<div class="wrap"><p class="sub" style="margin-top:40px">Налаштування закрито. Можна закрити вкладку.</p></div>';
  });
}
function save(){
  var msg=el("msg");msg.className="";msg.textContent="Зберігаю…";el("warns").textContent="";
  var body=JSON.stringify({token:el("token").value,name:el("name").value,display:(parseInt(el("display").value,10)||0),port:el("port").value});
  xhr("POST","/save",body,function(d){
    if(d&&d.ok){
      msg.className="ok";msg.textContent="Збережено й запущено ✓";
      if(d.warnings&&d.warnings.length){ el("warns").textContent="Увага: "+d.warnings.join("; "); }
      setTimeout(load,600);
    }else{
      msg.className="bad";msg.textContent=(d&&d.error)?d.error:"Помилка";
    }
  });
}
load();
</script>
</body>
</html>`
