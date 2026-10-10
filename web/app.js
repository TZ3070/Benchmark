const $ = id => document.getElementById(id);
const canvas = $('chart'), ctx = canvas.getContext('2d');
const state = {model:'',series:[],maxY:1,start:null,end:null,dragX:null,dragCurrent:null,plot:null,timer:null,startMs:Date.now()-21600000,endMs:Date.now()};
function fmt(v){return new Date(v).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit'});}
function iso(v){return new Date(v).toISOString();}
function setMessage(v){$('chartMessage').textContent=v||'';}
function sizeCanvas(){const r=canvas.getBoundingClientRect(),d=window.devicePixelRatio||1;canvas.width=Math.round(r.width*d);canvas.height=Math.round(r.height*d);ctx.setTransform(d,0,0,d,0,0);drawChart();}
function drawChart(){
 const w=canvas.clientWidth,h=canvas.clientHeight,pad={l:55,r:18,t:15,b:34},pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;
 ctx.clearRect(0,0,w,h);state.plot={left:pad.l,right:pad.l+pw,top:pad.t,bottom:pad.t+ph};ctx.font='10px DM Mono, monospace';ctx.textBaseline='middle';ctx.strokeStyle='#252d37';ctx.fillStyle='#798594';
 for(let i=0;i<5;i++){const y=pad.t+ph*i/4;ctx.beginPath();ctx.moveTo(pad.l,y);ctx.lineTo(pad.l+pw,y);ctx.stroke();ctx.textAlign='right';ctx.fillText((state.maxY*(1-i/4)).toFixed(state.maxY<1?2:0),pad.l-10,y);}
 const points=state.series;
 if(points.length){for(let i=0;i<5;i++){const x=pad.l+pw*i/4;ctx.textAlign=i===0?'left':i===4?'right':'center';ctx.fillText(fmt(state.startMs+(state.endMs-state.startMs)*i/4),x,h-14);}
  const coords=points.map(p=>({x:pad.l+(p.t-state.startMs)/(state.endMs-state.startMs)*pw,y:pad.t+ph-(p.v/state.maxY)*ph}));
  ctx.beginPath();coords.forEach((p,i)=>i?ctx.lineTo(p.x,p.y):ctx.moveTo(p.x,p.y));ctx.strokeStyle='#69e1b1';ctx.lineWidth=2;ctx.stroke();
  const grad=ctx.createLinearGradient(0,pad.t,0,pad.t+ph);grad.addColorStop(0,'rgba(105,225,177,.22)');grad.addColorStop(1,'rgba(105,225,177,0)');ctx.lineTo(coords[coords.length-1].x,pad.t+ph);ctx.lineTo(coords[0].x,pad.t+ph);ctx.closePath();ctx.fillStyle=grad;ctx.fill();
 }
 const a=state.dragX!==null?state.dragX:(state.start?pad.l+(state.start-state.startMs)/(state.endMs-state.startMs)*pw:null);
 const b=state.dragCurrent!==null?state.dragCurrent:(state.end?pad.l+(state.end-state.startMs)/(state.endMs-state.startMs)*pw:null);
 if(a!==null&&b!==null){const x=Math.min(a,b),width=Math.abs(b-a);ctx.fillStyle='rgba(105,225,177,.14)';ctx.fillRect(x,pad.t,width,ph);ctx.strokeStyle='#69e1b1';ctx.setLineDash([4,4]);ctx.strokeRect(x,pad.t,width,ph);ctx.setLineDash([]);}
}
function getTimeAt(x){const r=canvas.getBoundingClientRect(),p=state.plot;return state.startMs+Math.max(0,Math.min(1,(x-r.left-p.left)/(p.right-p.left)))*(state.endMs-state.startMs);}
async function loadSeries(){
 if(!state.model)return;const end=new Date(),start=new Date(Date.now()-6*60*60*1000);state.startMs=start.getTime();state.endMs=end.getTime();setMessage('正在查询 Prometheus…');
 try{const q=new URLSearchParams({model:state.model,start:start.toISOString(),end:end.toISOString()});const r=await fetch('/api/series?'+q),b=await r.json();if(!r.ok)throw Error(b.error||'Prometheus 查询失败');
  state.series=(b.result||[]).flatMap(s=>(s.values||[]).map(v=>({t:Number(v[0])*1000,v:Number(v[1])}))).sort((a,b)=>a.t-b.t);state.maxY=Math.max(...state.series.map(p=>p.v),1)*1.15;setMessage(state.series.length?'':'所选模型当前时间段没有 Prometheus 数据');drawChart();
 }catch(e){state.series=[];state.maxY=1;drawChart();setMessage(e.message);}
}
async function loadModels(){
 const r=await fetch('/api/models'),b=await r.json();if(!r.ok)throw Error(b.error||'读取模型失败');if(!b.source_models.length)throw Error('LiteLLM 数据库中暂时没有模型请求记录');
 b.source_models.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;$('sourceModel').append(o);});
 b.target_models.forEach(m=>{const o=document.createElement('option');o.value=m;o.textContent=m;$('targetModel').append(o);});
 if(!b.target_models.length)throw Error('LiteLLM 当前没有可用于回放的模型，请先配置目标模型');
 state.model=b.source_models[0];$('sourceModel').value=state.model;$('targetModel').value=b.target_models[0];await loadSeries();
}
canvas.addEventListener('pointerdown',e=>{if(!state.plot)return;canvas.setPointerCapture(e.pointerId);state.dragX=e.clientX-canvas.getBoundingClientRect().left;state.dragCurrent=state.dragX;drawChart();});
canvas.addEventListener('pointermove',e=>{if(state.dragX===null)return;state.dragCurrent=e.clientX-canvas.getBoundingClientRect().left;drawChart();});
canvas.addEventListener('pointerup',async e=>{
 if(state.dragX===null)return;const x0=state.dragX,x1=e.clientX-canvas.getBoundingClientRect().left;state.dragX=null;state.dragCurrent=null;
 if(Math.abs(x1-x0)<4){drawChart();return;}state.start=getTimeAt(canvas.getBoundingClientRect().left+Math.min(x0,x1));state.end=getTimeAt(canvas.getBoundingClientRect().left+Math.max(x0,x1));
 $('selectionPanel').classList.remove('hidden');$('selectedSource').textContent=state.model;$('selectedRange').textContent=fmt(state.start)+' — '+fmt(state.end);$('rangeLabel').textContent=fmt(state.start)+' → '+fmt(state.end);$('requestCount').textContent='读取请求数…';$('startRun').disabled=true;drawChart();await preview();
});
async function preview(){try{const q=new URLSearchParams({model:state.model,start:iso(state.start),end:iso(state.end)}),r=await fetch('/api/preview?'+q),b=await r.json();if(!r.ok)throw Error(b.error);$('requestCount').textContent=b.count.toLocaleString()+' 条请求';$('startRun').disabled=b.count===0;}catch(e){$('requestCount').textContent=e.message;$('startRun').disabled=true;}}
$('sourceModel').addEventListener('change',async e=>{state.model=e.target.value;state.start=null;state.end=null;$('selectionPanel').classList.add('hidden');await loadSeries();});
$('startRun').addEventListener('click',async()=>{
 if(!state.start||!state.end)return;const d={source_model:state.model,target_model:$('targetModel').value,start:iso(state.start),end:iso(state.end)};
 if(!confirm('确认开始回放？\n源模型：'+d.source_model+'\n目标模型：'+d.target_model+'\n时间：'+fmt(state.start)+' — '+fmt(state.end)+'\n请求数：'+$('requestCount').textContent))return;
 $('startRun').disabled=true;try{const r=await fetch('/api/runs',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(d)}),b=await r.json();if(!r.ok)throw Error(b.error);$('runPanel').classList.remove('hidden');$('runTitle').textContent=d.source_model+' → '+d.target_model;pollRun(b.run_id);}catch(e){alert(e.message);$('startRun').disabled=false;}
});
function esc(v){return String(v==null?'—':v).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
function metric(label,value,unit){return '<div class="metric"><small>'+label+'</small><strong>'+(value==null?'—':value)+(unit||'')+'</strong></div>';}
async function pollRun(id){
 clearInterval(state.timer);const update=async()=>{try{const r=await fetch('/api/runs/'+id),d=await r.json();$('runStatus').textContent=d.status;$('runStatus').className='status '+d.status;$('progressBar').style.width=(d.total?Math.round(d.completed/d.total*100):0)+'%';
  const labels={queued:'排队中',running:'运行中',failed:'失败',completed:'已完成'};$('runStatus').textContent=labels[d.status]||d.status;
  if(d.status==='running'||d.status==='queued')$('resultGrid').innerHTML=metric('已完成',(d.completed||0)+' / '+(d.total||0))+metric('失败数',d.errors||0);
  if(d.status==='failed'){$('resultGrid').innerHTML='<div class="metric"><small>运行错误</small><strong style="font-size:13px;color:#ff817e">'+esc(d.error)+'</strong></div>';clearInterval(state.timer);$('startRun').disabled=false;return;}
  if(d.status==='completed'){const baseP95=d.baseline.latency_ms_p95,change=baseP95?((baseP95-d.latency_ms_p95)/baseP95*100):null;
   $('resultGrid').innerHTML=metric('完成请求',d.completed)+metric('错误率',(d.error_rate*100).toFixed(2),'%')+metric('基线 P95 延迟',baseP95,' ms')+metric('回放 P95 延迟',d.latency_ms_p95,' ms')+metric('P95 延迟变化',change==null?'—':change.toFixed(1),'%');
   $('requestRows').innerHTML=(d.requests||[]).map(x=>'<tr><td>'+esc(x.source_request_id)+'</td><td>'+esc(x.status)+'</td><td>'+esc(x.dispatch_lag_ms)+' ms</td><td>'+(x.latency_ms==null?'—':x.latency_ms+' ms')+'</td><td>'+esc(x.input_tokens)+'</td><td>'+esc(x.output_tokens)+'</td><td>'+esc(x.error)+'</td></tr>').join('');clearInterval(state.timer);$('startRun').disabled=false;
  }
 }catch(e){clearInterval(state.timer);}};await update();state.timer=setInterval(update,1500);
}
window.addEventListener('resize',sizeCanvas);sizeCanvas();loadModels().catch(e=>setMessage(e.message));
