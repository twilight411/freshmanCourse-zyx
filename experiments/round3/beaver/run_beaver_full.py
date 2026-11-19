"""Resume BEAVER public evaluation in this workspace with a bounded key budget."""
from __future__ import annotations
import argparse, concurrent.futures, hashlib, importlib.util, json, os, re, socket, subprocess, sys, time
from collections import Counter
from datetime import datetime, timezone, timedelta
from itertools import zip_longest
from pathlib import Path
from beaver_score_policy import execution_issue

ROOT=Path(__file__).resolve().parent
SOURCE=Path(r'D:\BeaverBench')
OUT=ROOT/'beaver_full_run'; OUT.mkdir(exist_ok=True)
TZ=timezone(timedelta(hours=8))
SPLITS=('dw_real','neutron','nova','dw')
MODELS=('deepseek','qwen')
RATES={'deepseek':(20,80),'qwen':(10,30)}
def now():return datetime.now(TZ).isoformat(timespec='seconds')
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def jl(p):return [json.loads(x) for x in p.read_text(encoding='utf-8').splitlines() if x.strip()] if p.exists() else []
def save(p,obj):
    tmp=p.with_suffix(p.suffix+'.tmp');tmp.write_text(json.dumps(obj,ensure_ascii=False,indent=2),encoding='utf-8')
    for attempt in range(10):
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if attempt==9:raise
            # A dashboard reader may hold the old file briefly on Windows.
            time.sleep(0.1*(attempt+1))
def append(p,obj):
    with p.open('a',encoding='utf-8') as f:f.write(json.dumps(obj,ensure_ascii=False)+'\n');f.flush();os.fsync(f.fileno())
def load_module(name,file):
    spec=importlib.util.spec_from_file_location(name,file);module=importlib.util.module_from_spec(spec);sys.modules[name]=module;spec.loader.exec_module(module);return module
gen=load_module('beaver_original_generator',SOURCE/'run_whu_sample.py')
scorer=load_module('beaver_original_scorer',SOURCE/'score_whu_sample.py')
PROVIDER='thingo' if '--provider' in sys.argv and sys.argv[sys.argv.index('--provider')+1]=='thingo' else 'whu'
WHU_QWEN_RESUME = '--whu-qwen-resume' in sys.argv and PROVIDER == 'whu'
STATUS_FILE = 'qwen_flash_status.json' if WHU_QWEN_RESUME else 'status.json'
PROGRESS_FILE = 'qwen_flash_progress.html' if WHU_QWEN_RESUME else 'progress.html'
if PROVIDER=='thingo':
    secrets=read(ROOT/'.beaver_full/thingo_secrets.json')
    KEYS=[secrets['keys']['deepseek'],secrets['keys']['qwen']]
    KEY_LIMITS=[None,None]
    MODEL_SLOTS={'deepseek':0,'qwen':1}
    API_MODELS=secrets['models']
    API_ENDPOINT=secrets['endpoint']
    LEDGER_PATH=OUT/'thingo_usage.jsonl'
else:
    secrets=read(ROOT/'.beaver_full/secrets.json')
    KEYS=secrets['keys']
    KEY_LIMITS=secrets.get('credit_limits',[295]*len(KEYS))
    MODEL_SLOTS={}
    API_MODELS={name:gen.MODELS[name]['id'] for name in MODELS}
    API_ENDPOINT='https://maas.whu.edu.cn/v1/chat/completions'
    LEDGER_PATH=OUT/'new_key_usage.jsonl'
if len(KEY_LIMITS)!=len(KEYS):raise ValueError('credit_limits must match keys.')
ACTIVE_SLOTS={int(slot)-1 for slot in secrets.get('active_key_slots',range(1,len(KEYS)+1))}
if WHU_QWEN_RESUME:
    ACTIVE_SLOTS = {int(secrets['whu_qwen_resume_slot']) - 1}
if not ACTIVE_SLOTS or not ACTIVE_SLOTS.issubset(range(len(KEYS))):
    raise ValueError('active_key_slots must select existing configured keys.')
def safe_error(exc):
    value=str(exc)
    for key in KEYS:value=value.replace(key,'[redacted]')
    value=re.sub(r'sk-[A-Za-z0-9_.-]+','[redacted]',value)
    return value[:1600]

def should_disable_key(http_status,reason):
    text=reason.lower()
    return http_status in (401,402,403) or any(word in text for word in (
        'insufficient','quota','balance','credit','budget_exceeded',
        'budget has been exceeded','budget exceeded','积分','余额','额度','欠费'))
datasets={sp:read(SOURCE/'data'/sp/'dev.json') for sp in SPLITS}
tables={sp:read(SOURCE/'data'/('dw' if sp=='dw_real' else sp)/'dev_tables.json') for sp in SPLITS}
questions={str(q['id']):q for qs in datasets.values() for q in qs}
predictions={}; scores={}; prompts={}
for sp in SPLITS:
    for model in MODELS:
        target=OUT/f'{sp}_{model}'; target.mkdir(exist_ok=True)
        for filename in ['predictions.jsonl','prompts.jsonl']:
            if not (target/filename).exists():
                # Copy only the current effective row per ID; historical charges belong to old keys.
                rows={str(x['id']):x for x in jl(SOURCE/'runs'/f'{sp}_{model}'/filename)}
                for row in rows.values():append(target/filename,row)
        if not (target/'scores.jsonl').exists():
            for row in read(SOURCE/'runs'/f'{sp}_{model}'/'scores.json'):append(target/'scores.jsonl',row)
        predictions[sp,model]={str(x['id']):x for x in jl(target/'predictions.jsonl')}
        prompts[sp,model]={str(x['id']):x for x in jl(target/'prompts.jsonl')}
        scores[sp,model]={str(x['id']):x for x in jl(target/'scores.jsonl')}
ledger=jl(LEDGER_PATH)
usage=[0.0]*len(KEYS); blocked={}; requests_count=0
for row in ledger:
    usage[row['key_slot']-1]+=row.get('estimated_credits',0)
    if row.get('disable_key'):blocked[row['key_slot']-1]=row.get('reason','unavailable')
def status(state,message='',current_tasks=None):
    runs=[]
    for sp in SPLITS:
        for model in MODELS:
            rs=list(scores[sp,model].values()); c=Counter(x['status'] for x in rs)
            runs.append({'split':sp,'model':model,'total':len(datasets[sp]),'predictions':sum(bool(x.get('predicted_sql')) for x in predictions[sp,model].values()),'scores':len(rs),'counts':dict(c)})
    snapshot={'state':state,'message':message,'provider':PROVIDER,'api_models':API_MODELS,'updated_beijing':now(),'pid':os.getpid(),'keys':[{'slot':i+1,'active':i in ACTIVE_SLOTS,'estimated_credits_used':round(u,6),'estimated_limit':KEY_LIMITS[i],'blocked':blocked.get(i)} for i,u in enumerate(usage)],'runs':runs,'current_tasks':current_tasks or [],'new_requests_this_process':requests_count,'rates_are_historical_estimates':True}
    try:save(OUT/STATUS_FILE,snapshot)
    except PermissionError:
        # The prediction and usage ledgers are already durable. A transient UI
        # read lock must not terminate paid model generation.
        print(f'[{now()}] Status JSON is locked; continuing with saved answer checkpoints.',flush=True)
    render_progress(snapshot)

def render_progress(s):
    import html
    esc=lambda x:html.escape(str(x),quote=True)
    totals={model:{'scored':sum(r['scores'] for r in s['runs'] if r['model']==model),'total':sum(r['total'] for r in s['runs'] if r['model']==model)} for model in MODELS}
    overall_scored=sum(x['scored'] for x in totals.values());overall_total=sum(x['total'] for x in totals.values())
    def bar(done,total):
        pct=100*done/total if total else 0
        return f'<div class="bar"><span style="width:{pct:.2f}%"></span></div><small>{done:,} / {total:,} · {pct:.1f}%</small>'
    model_cards=''.join(f'<article class="card"><div class="eyebrow">{esc(API_MODELS[m])}</div><div class="big">{totals[m]["scored"]:,}<small> / {totals[m]["total"]:,}</small></div>{bar(totals[m]["scored"],totals[m]["total"])}</article>' for m in MODELS)
    current=''.join(f'<article class="task"><b>{esc(API_MODELS[t["model"]])} · {esc(t["split"])} · {esc(t["id"])}</b><p>{esc(t["question"])}</p></article>' for t in s['current_tasks']) or '<p class="muted">当前没有正在请求的题目。</p>'
    def accuracy(counts):
        checked=counts.get('correct',0)+counts.get('wrong',0)
        return f'{100*counts.get("correct",0)/checked:.2f}%' if checked else '—'
    rows=[];all_counts=Counter()
    for r in s['runs']:
        c=r['counts'];all_counts.update(c)
        rows.append('<tr><td>'+esc(r['split'])+'</td><td>'+esc(API_MODELS[r['model']])+'</td><td>'+bar(r['scores'],r['total'])+'</td><td>'+esc(c.get('correct',0))+'</td><td>'+esc(c.get('wrong',0))+'</td><td>'+esc(c.get('gold_error',0))+'</td><td>'+esc(c.get('execution_unresolved',0))+'</td><td><strong>'+accuracy(c)+'</strong></td></tr>')
    total_row='<tr class="total"><td>四组总体</td><td>两模型合计</td><td>'+bar(overall_scored,overall_total)+'</td><td>'+esc(all_counts.get('correct',0))+'</td><td>'+esc(all_counts.get('wrong',0))+'</td><td>'+esc(all_counts.get('gold_error',0))+'</td><td>'+esc(all_counts.get('execution_unresolved',0))+'</td><td><strong>'+accuracy(all_counts)+'</strong></td></tr>'
    active=[k for k in s['keys'] if k['active']]
    credit=('平台余额未知；本地记录只统计 token，不代表实际账单' if PROVIDER=='thingo' else ' / '.join('Key '+str(k['slot'])+'：'+format(k['estimated_credits_used'],'.3f')+' 积分估算' for k in active))
    page=f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta http-equiv="refresh" content="5"><meta name="viewport" content="width=device-width,initial-scale=1"><title>BEAVER 实时进度</title><style>
    :root{{color-scheme:light}}*{{box-sizing:border-box}}body{{margin:0;background:#f4f7fb;color:#17324c;font:15px/1.55 system-ui,sans-serif}}main{{max-width:1150px;margin:auto;padding:28px 20px 50px}}header{{display:flex;justify-content:space-between;gap:20px;align-items:start}}h1{{margin:0;font-size:28px}}h2{{font-size:18px;margin:30px 0 12px}}.muted,small{{color:#617387}}.eyebrow{{color:#58718a;font-size:13px}}.badge{{display:inline-block;padding:5px 11px;border-radius:30px;background:#d8f4e9;color:#126646;font-weight:700}}.badge.stop{{background:#fff0d5;color:#855200}}.grid{{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:14px;margin-top:22px}}.card,.task,.panel{{background:white;border:1px solid #dbe5ee;border-radius:15px;padding:20px;box-shadow:0 4px 18px #14314e0a}}.big{{font-size:30px;font-weight:750;line-height:1.3;margin:5px 0}}.big small{{font-size:16px;font-weight:400}}.bar{{height:10px;background:#e9eff5;border-radius:20px;overflow:hidden;margin:10px 0 4px;min-width:120px}}.bar span{{display:block;height:100%;background:linear-gradient(90deg,#2b77c8,#43c5a2);border-radius:20px}}.tasks{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}.task p{{margin:8px 0 0;max-height:5em;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;border-bottom:1px solid #e8eef4;padding:12px 10px;vertical-align:middle}}th{{color:#60748a;font-size:13px}}td:nth-child(3){{min-width:180px}}tfoot .total{{background:#eaf4fb;font-weight:700}}tfoot td{{border-top:2px solid #b8d4e7}}.footer{{margin-top:24px;color:#617387}}@media(max-width:750px){{.grid,.tasks{{grid-template-columns:1fr}}header{{display:block}}.panel{{overflow:auto}}}}
    </style><main><header><div><div class="eyebrow">{esc('Thingo UniAPI' if PROVIDER=='thingo' else '武大 MaaS')} · BEAVER 公开四组</div><h1>实时续跑进度</h1><p class="muted">每 5 秒自动刷新 · 数据更新时间：{esc(s['updated_beijing'])}</p></div><span class="badge {'stop' if s['state']!='running' else ''}">{esc(s['state'])}</span></header>
    <section class="grid"><article class="card"><div class="eyebrow">两模型总计 · 含历史结果</div><div class="big">{overall_scored:,}<small> / {overall_total:,}</small></div>{bar(overall_scored,overall_total)}</article>{model_cards}</section>
    <h2>当前处理的题目</h2><div class="tasks">{current}</div><h2>分组进度</h2><p class="muted">正确率 = 正确 ÷（正确＋失分）；仅统计当前已可核验的题目，参考异常和执行待复核单列。</p><div class="panel"><table><thead><tr><th>数据组</th><th>模型</th><th>已处理 / 总数</th><th>正确</th><th>失分</th><th>参考异常</th><th>执行待复核</th><th>正确率</th></tr></thead><tbody>{''.join(rows)}</tbody><tfoot>{total_row}</tfoot></table></div>
    <div class="footer">最近状态：{esc(s['message'])}<br>{esc(credit)}；积分仅为历史费率估算，不是平台账单。当前进程新增请求：{s['new_requests_this_process']}。</div></main></html>'''
    temp=OUT/(PROGRESS_FILE+'.tmp');temp.write_text(page,encoding='utf-8')
    for attempt in range(5):
        try:
            temp.replace(OUT/PROGRESS_FILE)
            break
        except PermissionError:
            if attempt==4:
                # Windows viewers may briefly lock the HTML file. The JSON state
                # and answer checkpoints are already saved; a display miss must
                # never stop model generation or corrupt an evaluation.
                print(f'[{now()}] Progress HTML is locked; continuing with saved JSON state.',flush=True)
            else:time.sleep(0.2*(attempt+1))
def score_prediction(sp,model,q,pred,password,force=False):
    qid=str(q['id']); old=scores[sp,model].get(qid)
    digest=hashlib.sha256(pred.get('predicted_sql','').encode()).hexdigest()
    if old and old.get('sql_sha256')==digest and not force:return old.get('status')
    db='dw' if sp=='dw_real' else sp
    gold,gold_df=scorer.execute(q['sql'],db,password)
    result,pred_df=scorer.execute(pred.get('predicted_sql',''),db,password)
    if gold.get('error'):
        state,comparison='gold_error','Gold SQL failed'
    elif execution_issue(result.get('error')):
        state,comparison='execution_unresolved',result['error']
    elif result.get('error'):
        state,comparison='wrong',result['error']
    else:
        match,comparison=scorer.compare_results(pred_df,gold_df);state='correct' if match else 'wrong'
    missing=sorted(set(q.get('tables') or [])-set(prompts[sp,model].get(qid,{}).get('selected_tables') or []))
    row={'id':qid,'status':state,'comparison':comparison,'score_version':4,'sql_sha256':digest,'retrieval_missing_tables':missing,'error_type':('模型未看到参考所需表（检索缺失）' if missing else scorer.classify(pred.get('predicted_sql',''),result,gold)) if state=='wrong' else execution_issue(result.get('error')) if state=='execution_unresolved' else None,'diagnosis':scorer.diagnose(pred.get('predicted_sql',''),q['sql'],result,gold) if state=='wrong' else [],'difference':scorer.difference(pred_df,gold_df) if state=='wrong' and not result.get('error') and not gold.get('error') else None,'ai_result':result,'gold_result':gold,'scored_beijing':now()}
    append(OUT/f'{sp}_{model}'/'scores.jsonl',row);scores[sp,model][qid]=row
    return state
def export():
    if WHU_QWEN_RESUME:
        return  # The shared canonical ledgers are appended as each answer is scored.
    for sp in SPLITS:
        for model in MODELS:
            c=Counter(x['status'] for x in scores[sp,model].values()); n=c['correct']+c['wrong']
            save(OUT/f'{sp}_{model}'/'summary.json',{'split':sp,'model':model,'total':len(datasets[sp]),'scored':sum(c.values()),'counts':dict(c),'ex_percent':round(100*c['correct']/n,4) if n else None,'updated_beijing':now()})
            save(OUT/f'{sp}_{model}'/'scores.json',list(scores[sp,model].values()))

def finish_with_replay(state,message):
    status(state,message);export()
    if WHU_QWEN_RESUME:
        return
    status('replaying_scores','Model requests stopped; replaying saved answers without API calls.');export()
    log_path=OUT/'replay_after_quota.log'
    with log_path.open('ab') as log:
        completed=subprocess.run([sys.executable,'-u',str(ROOT/'replay_beaver_saved_answers.py')],cwd=ROOT,stdout=log,stderr=subprocess.STDOUT)
    replay_status=OUT/'replay_after_quota'/'status.json'
    replay_state=read(replay_status).get('state') if replay_status.exists() else 'missing'
    if completed.returncode==0 and replay_state=='completed':
        status(state,message+' Saved-answer replay completed; see replay_after_quota.');export()
    else:
        status('replay_failed',f'Offline replay did not complete (exit {completed.returncode}, state {replay_state}); see replay_after_quota.log.');export()
def request_one(sp,model,q,prompt,slot):
    import urllib.request, urllib.error
    try:
        body={'model':API_MODELS[model],'messages':[{'role':'user','content':prompt}],'max_tokens':4096,'chat_template_kwargs':{'enable_thinking':False}}
        request=urllib.request.Request(API_ENDPOINT,data=json.dumps(body).encode('utf-8'),headers={'Authorization':'Bearer '+KEYS[slot],'Content-Type':'application/json'})
        with urllib.request.urlopen(request,timeout=180) as resp:response=json.load(resp)
        choice=response['choices'][0];text=choice['message'].get('content') or '';u=response.get('usage')
        return {'id':str(q['id']),'db':q['db'],'model':API_MODELS[model],'provider':PROVIDER,'timestamp_beijing':now(),'raw_response':text,'predicted_sql':gen.extract_sql(text),'api_model':response.get('model'),'finish_reason':choice.get('finish_reason'),'input_tokens':u.get('prompt_tokens',0) if u else 0,'output_tokens':u.get('completion_tokens',0) if u else 0,'key_slot':slot+1,'usage_missing':u is None}
    except Exception as exc:
        reason=safe_error(exc)
        if isinstance(exc,urllib.error.HTTPError):
            try:reason+=' '+safe_error(exc.read(4000).decode('utf-8',errors='replace'))
            except Exception:pass
        return {'id':str(q['id']),'db':q['db'],'model':API_MODELS[model],'provider':PROVIDER,'timestamp_beijing':now(),'api_error':reason,'http_status':getattr(exc,'code',None),'predicted_sql':'','input_tokens':0,'output_tokens':0,'key_slot':slot+1}
def main():
    global requests_count
    parser=argparse.ArgumentParser();parser.add_argument('--provider',choices=('whu','thingo'),default='whu');parser.add_argument('--initialize-only',action='store_true');parser.add_argument('--max-new-requests',type=int,default=0);parser.add_argument('--only-model',choices=MODELS);parser.add_argument('--rescore-connection-errors',action='store_true');parser.add_argument('--whu-qwen-resume', action='store_true');args=parser.parse_args()
    if args.whu_qwen_resume and (not WHU_QWEN_RESUME or args.only_model != 'qwen' or args.rescore_connection_errors):
        parser.error('--whu-qwen-resume requires --provider whu --only-model qwen for generation')
    status('initialized','Loaded saved checkpoints; existing key usage and budgets preserved.');export()
    if args.initialize_only:return
    # Check MySQL before starting any paid generation.
    password=scorer.password()
    try:
        import mysql.connector
        conn=mysql.connector.connect(host='127.0.0.1',port=33306,user='root',password=password,connection_timeout=10)
        conn.close()
    except Exception as exc:
        message=safe_error(exc)
        state='blocked_permissions' if isinstance(exc,PermissionError) or any(code in message for code in ('Errno 13','WinError 10013')) else 'blocked_database'
        status(state,message);export();return
    if args.rescore_connection_errors:
        rescored=0
        for sp in SPLITS:
            for model in MODELS:
                for q in datasets[sp]:
                    qid=str(q['id']);old=scores[sp,model].get(qid);pred=predictions[sp,model].get(qid)
                    if not old or not pred or not pred.get('predicted_sql'):continue
                    if execution_issue(old.get('gold_result',{}).get('error'))!='database_connection' and old.get('error_type')!='database_connection':continue
                    score_prediction(sp,model,q,pred,password,force=True);rescored+=1
                    print(f'[{now()}] rescored {sp}/{model}/{qid}: {scores[sp,model][qid]["status"]}',flush=True)
        status('rescore_complete',f'Rescored {rescored} saved predictions with prior database connection failures.');export();return
    # Complete scoring for any generation saved before an interruption.
    for sp in SPLITS:
        for model in MODELS:
            if WHU_QWEN_RESUME and model != 'qwen':
                continue
            for q in datasets[sp]:
                pred=predictions[sp,model].get(str(q['id']))
                if pred and not pred.get('api_error'):
                    if WHU_QWEN_RESUME and (not pred.get('predicted_sql') or (str(q['id']) in scores[sp,model] and not pred.get('whu_qwen_resume'))):
                        continue
                    score_prediction(sp,model,q,pred,password)
    queues={model:[] for model in MODELS}
    for sp in SPLITS:
        for q in datasets[sp]:
            for model in MODELS:
                if args.only_model and model!=args.only_model:continue
                old=predictions[sp,model].get(str(q['id']))
                if not old or old.get('api_error') or (WHU_QWEN_RESUME and not old.get('predicted_sql')):queues[model].append((sp,model,q))
    queue=[task for pair in zip_longest(*(queues[m] for m in MODELS)) for task in pair if task]
    status('running',f'{len(queue)} model-question tasks remaining.');export()
    failures=0;last_export=time.monotonic()
    for start in range(0,len(queue),2):
        tasks=[]; reserved=[0.0]*len(KEYS)
        for sp,model,q in queue[start:start+2]:
            if args.max_new_requests and requests_count+len(tasks)>=args.max_new_requests:break
            prompt,selected=gen.build_prompt(q,tables[sp],15)
            # Conservative reservation: 48k schema chars plus max output, no hidden SDK retries.
            ir,orr=RATES[model];estimate=((len(prompt)+4096)*ir+4096*orr)/1_000_000
            choices=[i for i in ACTIVE_SLOTS if i not in blocked and (model not in MODEL_SLOTS or i==MODEL_SLOTS[model]) and (KEY_LIMITS[i] is None or usage[i]+reserved[i]+estimate<=KEY_LIMITS[i])]
            if not choices:continue
            slot=min(choices,key=lambda i:usage[i]+reserved[i]);reserved[slot]+=estimate
            qid=str(q['id']);pr={'id':qid,'db':q['db'],'question':q['question'],'prompt':prompt,'selected_tables':selected}
            if qid not in prompts[sp,model]:append(OUT/f'{sp}_{model}'/'prompts.jsonl',pr)
            prompts[sp,model][qid]=pr;tasks.append((sp,model,q,prompt,slot,estimate))
        if not tasks:
            if ACTIVE_SLOTS.issubset(blocked):
                finish_with_replay('paused_quota','All selected keys report quota/authentication failure.');return
            status('paused_budget','No key has enough estimated remaining credit for another bounded request.');export();return
        current_tasks=[{'split':sp,'model':model,'id':str(q['id']),'question':q['question']} for sp,model,q,*_ in tasks]
        status('running','Calling '+', '.join(t['id'] for t in current_tasks),current_tasks)
        database_failed=False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures={pool.submit(request_one,*task[:5]):task for task in tasks}
            for future in concurrent.futures.as_completed(futures):
                sp,model,q,prompt,slot,reservation=futures[future];row=future.result();requests_count+=1
                ir,orr=RATES[model];cost=(row['input_tokens']*ir+row['output_tokens']*orr)/1_000_000
                # A timed-out request may still have been billed. Reserve its full bounded allowance.
                reason=row.get('api_error','');http=row.get('http_status')
                disable=should_disable_key(http,reason)
                if row.get('api_error') or row.get('usage_missing'):cost=reservation
                append(LEDGER_PATH,{'at':now(),'id':row['id'],'split':sp,'model':model,'api_model':API_MODELS[model],'provider':PROVIDER,'key_slot':slot+1,'input_tokens':row['input_tokens'],'output_tokens':row['output_tokens'],'estimated_credits':cost,'estimate_reserved_for_unknown_billing':bool(row.get('api_error') or row.get('usage_missing')),'disable_key':disable,'reason':reason if disable else ''})
                usage[slot]+=cost
                if disable:blocked[slot]=reason or str(http)
                if WHU_QWEN_RESUME:
                    row['whu_qwen_resume'] = True
                append(OUT/f'{sp}_{model}'/'predictions.jsonl',row);predictions[sp,model][row['id']]=row
                if row.get('api_error'):
                    failures+=1
                    print(f'[{now()}] {sp}/{model}/{row["id"]}: request failed (HTTP {http}); saved checkpoint.',flush=True)
                else:
                    failures=0;score_prediction(sp,model,q,row,password)
                    print(f'[{now()}] {sp}/{model}/{row["id"]}: saved and scored; key {slot+1}, estimated usage {usage[slot]:.3f}.',flush=True)
                    assessed=scores[sp,model].get(row['id'],{})
                    if execution_issue(assessed.get('gold_result',{}).get('error'))=='database_connection' or assessed.get('error_type')=='database_connection':
                        database_failed=True
                current_tasks=[t for t in current_tasks if not (t['id']==row['id'] and t['model']==model)]
                status('running',f'Last completed: {sp}/{model}/{row["id"]}',current_tasks)
        if database_failed:status('blocked_database','Database connection failed during scoring; predictions were saved.');export();return
        if ACTIVE_SLOTS.issubset(blocked):finish_with_replay('paused_quota','All selected keys report quota/authentication failure.');return
        if failures>=4:status('paused_api_errors','Four consecutive API failures; stopped to avoid uncontrolled retries.');export();return
        if args.max_new_requests and requests_count>=args.max_new_requests:status('paused_probe','Requested limited smoke run completed.');export();return
        if time.monotonic()-last_export>=60:export();last_export=time.monotonic()
    if WHU_QWEN_RESUME:
        remaining = sum(not row or row.get('api_error') or not row.get('predicted_sql')
                        for sp in SPLITS for q in datasets[sp]
                        for row in [predictions[sp,'qwen'].get(str(q['id']))])
        status('paused_incomplete' if remaining else 'completed', f'{remaining} qwen-flash tasks still lack SQL.')
        return
    remaining=sum(not predictions[sp,model].get(str(q['id'])) or bool(predictions[sp,model][str(q['id'])].get('api_error')) for sp in SPLITS for model in MODELS for q in datasets[sp])
    if not remaining:finish_with_replay('completed','All model-question tasks have an answer.')
    else:status('paused_incomplete',f'Generation pass finished; {remaining} model-question tasks still lack SQL.');export()
if __name__=='__main__':
    # A loopback lock prevents two processes spending the same budget concurrently.
    lock=socket.socket()
    try:lock.bind(('127.0.0.1', 37834 if WHU_QWEN_RESUME else 37831))
    except PermissionError as exc:
        status('blocked_permissions','Local socket access was denied; model API and database connections cannot start. '+safe_error(exc));export()
        raise SystemExit('Socket permission denied; no generation was started.')
    except OSError as exc:
        if exc.errno in (98,10048) or getattr(exc,'winerror',None)==10048:
            raise SystemExit('Another full-run worker is already active; not starting a second worker.')
        status('failed',safe_error(exc));export();raise
    try:main()
    except Exception as exc:
        status('failed',safe_error(exc));export();raise SystemExit(type(exc).__name__+': '+safe_error(exc))
    finally:lock.close()
