// LG전자 간극 그래프 샘플의 그리기 코드를 회사 공통으로 옮긴 것. D, UNIT은 페이지가 넣는다.
const S=D.series, YR=S.year, N=YR.length, NOW=YR.indexOf(2026), XL=YR.map(y=>y>2026?String(y).slice(2)+'E':y===2026?'26(12M)':String(y).slice(2));
const DEF=[{k:'매출',c:'acc',t:'i',u:UNIT,off:true},{k:'영업이익',c:'good',t:'i',u:UNIT},{k:'순이익(지배)',c:'mid',t:'i',u:UNIT},
 {k:'시가총액',c:'warn',t:'i',u:UNIT,w:3},{k:'EPS',c:'vio',t:'i',u:'원',off:true},{k:'영업이익률',c:'ink2',t:'p',u:'%'},{k:'ROE',c:'red',t:'p',u:'%'},{k:'부채비율',c:'muted',t:'p',u:'%',off:true},
 {k:'주가',c:'px',t:'v',u:'원',w:2.6},{k:'종합',c:'iv',t:'v',u:'원',w:3.2},{k:'S-RIM',c:'srim',t:'v',u:'원',w:1.5},{k:'DCF',c:'dcf',t:'v',u:'원',w:1.5},{k:'DDM',c:'ddm',t:'v',u:'원',w:1.5},{k:'상대가치',c:'rel',t:'v',u:'원',w:1.5}];
const VK=DEF.filter(d=>d.t==='v').map(d=>d.k);
const st={on:Object.fromEntries(DEF.map(d=>[d.k,d.t==='v'])),base:2016};
const NS='http://www.w3.org/2000/svg', tip=document.getElementById('tip');
const fmt=(v,u)=>v==null?'—':(u==='원'?Math.round(v).toLocaleString()+'원':u==='%'?v.toFixed(1)+'%':Math.round(v).toLocaleString()+u);
function el(t,a,p){const e=document.createElementNS(NS,t);for(const k in a)e.setAttribute(k,a[k]);if(p)p.appendChild(e);return e}
function txt(p,x,y,s,a){const e=el('text',Object.assign({x,y},a||{}),p);e.textContent=s;return e}
function ctl(){const c=document.getElementById('ctl');c.replaceChildren();
 DEF.forEach(d=>{const b=document.createElement('button');b.className=st.on[d.k]?'on':'';const sw=document.createElement('span');sw.className='sw';sw.style.background=`var(--${d.c})`;b.append(sw,d.k);b.onclick=()=>{st.on[d.k]=!st.on[d.k];ctl();draw1()};c.appendChild(b)});
 const vb=document.createElement('button');vb.textContent='주당 가치·주가만';vb.onclick=()=>{DEF.forEach(d=>st.on[d.k]=d.t==='v');ctl();draw1()};c.appendChild(vb);
 const ab=document.createElement('button');ab.textContent='전부 켜기';ab.onclick=()=>{DEF.forEach(d=>st.on[d.k]=d.k!=='부채비율');ctl();draw1()};c.appendChild(ab);
 const bb=document.getElementById('base');bb.querySelectorAll('button').forEach(x=>x.remove());
 [2016,2020,2025].forEach(y=>{const b=document.createElement('button');b.className=st.base===y?'on':'';b.textContent=y+' = 100';b.onclick=()=>{st.base=y;ctl();draw1()};bb.appendChild(b)})}
function draw1(){const box=document.getElementById('c1');box.replaceChildren();const W=880,H=440,L=56,R=58,T=20,B=34;
 const s=el('svg',{viewBox:`0 0 ${W} ${H}`},box);const bi=YR.indexOf(st.base);const X=i=>L+(W-L-R)*i/(N-1);
 const den=k=>VK.includes(k)?S['주가'][bi]:S[k][bi];
 const idx=(k,v)=>{const b=den(k);return v==null||b==null||b<=0||v<=0?null:v/b*100};
 let iv=[],pv=[];
 DEF.forEach(d=>{if(!st.on[d.k])return;S[d.k].forEach(v=>{if(v==null)return;if(d.t==='p')pv.push(v);else{const z=idx(d.k,v);if(z)iv.push(z)}})});
 if(st.on['종합'])Object.values(D.vband).forEach(b=>{iv.push(idx('종합',b.lo),idx('종합',b.hi))});
 if(!iv.length)iv=[50,200];
 const lo=Math.min(...iv)*.85,hi=Math.max(...iv)*1.15,Y=v=>T+(H-T-B)*(1-(Math.log(v)-Math.log(lo))/(Math.log(hi)-Math.log(lo)));
 const pmin=Math.min(0,...pv),pmax=pv.length?Math.max(...pv)*1.12:10,P=v=>T+(H-T-B)*(1-(v-pmin)/(pmax-pmin));
 el('rect',{x:X(NOW+.5),y:T,width:X(N-1)-X(NOW+.5)+8,height:H-T-B,style:'fill:var(--acc-soft);opacity:.35'},s);
 txt(s,(X(NOW+.5)+X(N-1))/2,T+14,'추정(기본 시나리오)',{'text-anchor':'middle',style:'fill:var(--acc);font-size:12px'});
 const step=Math.pow(10,Math.floor(Math.log10(lo)));[1,2,5].forEach(m=>{for(let v=step*m;v<hi;v*=10){if(v<lo)continue;el('line',{x1:L,x2:W-R,y1:Y(v),y2:Y(v),style:'stroke:var(--grid)'},s);txt(s,L-6,Y(v)+4,v>=1000?(v/1000)+'천':String(+v.toPrecision(3)),{'text-anchor':'end',style:'fill:var(--muted);font-size:11px'})}});
 if(pv.length){const span=pmax-pmin,ps=span>120?50:span>40?10:span>16?5:2;for(let v=Math.ceil(pmin/ps)*ps;v<pmax;v+=ps)txt(s,W-R+6,P(v)+4,v+'%',{style:'fill:var(--muted);font-size:11px'})}
 XL.forEach((l,i)=>txt(s,X(i),H-B+18,l,{'text-anchor':'middle',style:'fill:var(--ink2);font-size:12px'}));
 if(st.on['종합']){const v=S['종합'],up=[[X(NOW),Y(idx('종합',v[NOW]))]],dn=[[X(NOW),Y(idx('종합',v[NOW]))]];
  Object.keys(D.vband).forEach(y=>{const i=YR.indexOf(+y);up.push([X(i),Y(idx('종합',D.vband[y].hi))]);dn.unshift([X(i),Y(idx('종합',D.vband[y].lo))])});
  if(idx('종합',v[NOW]))el('polygon',{points:up.concat(dn).map(p=>p.join(',')).join(' '),style:'fill:var(--iv-soft);opacity:.8'},s)}
 DEF.forEach(d=>{if(!st.on[d.k])return;const v=S[d.k],f=i=>{if(d.t==='p')return v[i]==null?null:P(v[i]);const z=idx(d.k,v[i]);return z?Y(z):null};
  const main=d.k==='주가'||d.k==='종합',wd=d.w||(d.t==='p'?1.6:2.4);
  const run=(a,b,dash)=>{let pts=[];const flush=()=>{if(pts.length>1)el('polyline',{points:pts.map(p=>p.join(',')).join(' '),style:`fill:none;stroke:var(--${d.c});stroke-width:${wd};stroke-linecap:round;stroke-linejoin:round;${dash?'stroke-dasharray:6 4;':''}${main||d.t==='i'?'':'opacity:.8'}`},s);pts=[]};
   for(let i=a;i<=b;i++){const y=f(i);if(y!=null)pts.push([X(i),y]);else flush()}flush()};
  run(0,NOW,d.t==='p');run(NOW,N-1,true);
  for(let i=0;i<=NOW;i++){const y=f(i);if(y!=null)el('circle',{cx:X(i),cy:y,r:d.k==='주가'?3.6:d.k==='종합'?3.4:d.t==='p'?2.4:2.6,style:`fill:var(--${d.c})`},s)}
  if(d.k==='주가'){const y=f(NOW);if(y)txt(s,X(NOW)+8,y+16,'주가',{style:'fill:var(--px);font-weight:700;font-size:12px'})}
  if(d.k==='종합'){const y=f(N-1);if(y)txt(s,X(N-1)-4,y-8,'종합가치',{'text-anchor':'end',style:'fill:var(--iv);font-weight:700;font-size:11px'})}});
 const vl=el('line',{y1:T,y2:H-B,style:'stroke:var(--ink2);opacity:0'},s);
 s.addEventListener('mousemove',e=>{const r=s.getBoundingClientRect(),x=(e.clientX-r.left)*W/r.width,i=Math.max(0,Math.min(N-1,Math.round((x-L)/(W-L-R)*(N-1))));
  vl.setAttribute('x1',X(i));vl.setAttribute('x2',X(i));vl.style.opacity=.4;
  const lines=DEF.filter(d=>st.on[d.k]&&S[d.k][i]!=null).map(d=>`${d.k} ${fmt(S[d.k][i],d.u)}`);
  if(i>NOW&&st.on['종합'])lines.push(`종합가치 범위 ${fmt(D.vband[YR[i]].lo,'원')}~${fmt(D.vband[YR[i]].hi,'원')}`);
  tip.replaceChildren();const h=document.createElement('b');h.textContent=`${YR[i]}${i>NOW?'E (기본 시나리오)':i===NOW?' 최근 12개월':''}`;tip.appendChild(h);
  lines.forEach(t=>{tip.appendChild(document.createElement('br'));tip.appendChild(document.createTextNode(t))});
  if(VK.some(k=>st.on[k])){tip.appendChild(document.createElement('br'));const n=document.createElement('span');n.style.opacity=.75;n.textContent=`주당 지표는 ${st.base}년 주가 ${fmt(S['주가'][bi],'원')} = 100`;tip.appendChild(n)}
  tip.style.opacity=1;tip.style.left=Math.min(e.pageX+14,window.innerWidth-260)+'px';tip.style.top=(e.pageY+10)+'px'});
 s.addEventListener('mouseleave',()=>{tip.style.opacity=0;vl.style.opacity=0})}
function draw2(){const box=document.getElementById('c2');const W=880,H=260,L=40,R=120,T=16,B=40;const s=el('svg',{viewBox:`0 0 ${W} ${H}`},box);
 const ys=Object.keys(D.gap),g=ys.map(y=>D.gap[y]),lab=ys.map(y=>y.slice(2));if(D.gap_now!=null){g.push(D.gap_now);lab.push('지금/12M')}
 const mx=Math.max(...g,D.G.p75)*1.15,n=g.length,bw=(W-L-R)/n,Y=v=>T+(H-T-B)*(1-v/mx);
 const gs=mx>60?20:mx>24?10:mx>12?4:2;for(let v=gs;v<mx;v+=gs){el('line',{x1:L,x2:W-R,y1:Y(v),y2:Y(v),style:'stroke:var(--grid)'},s);txt(s,L-6,Y(v)+4,v+'배',{'text-anchor':'end',style:'fill:var(--muted);font-size:11px'})}
 g.forEach((v,i)=>{const isNow=D.gap_now!=null&&i===n-1;el('rect',{x:L+bw*i+bw*.18,y:Y(v),width:bw*.64,height:H-B-Y(v),rx:3,style:`fill:var(--${isNow?'warn':'acc'});opacity:${isNow?.85:.75}`},s);
  txt(s,L+bw*i+bw/2,Y(v)-5,v.toFixed(1),{'text-anchor':'middle',style:'fill:var(--ink2);font-size:11.5px'});txt(s,L+bw*i+bw/2,H-B+16,lab[i],{'text-anchor':'middle',style:'fill:var(--ink2);font-size:11px'})});
 let ly=[];[['p25','25%'],['med','가운데'],['p75','75%']].forEach(([k,nm])=>{const v=D.G[k];el('line',{x1:L,x2:W-R,y1:Y(v),y2:Y(v),style:'stroke:var(--warn);stroke-dasharray:5 4'},s);let y=Y(v)+4;while(ly.some(q=>Math.abs(q-y)<13))y-=13;ly.push(y);txt(s,W-R+6,y,`${nm} ${v.toFixed(1)}배`,{style:'fill:var(--warn);font-size:11.5px'})})}
ctl();draw1();draw2();
