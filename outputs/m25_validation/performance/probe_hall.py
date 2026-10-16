"""Exact count MILP with Hall window cuts and original-binary restoration."""
from pathlib import Path
from copy import deepcopy
from time import perf_counter
from types import SimpleNamespace
import hashlib,json,sys
import numpy as np
import pandas as pd
from scipy.optimize import linear_sum_assignment
from scipy.optimize._highspy import _core as h
ROOT=Path(__file__).resolve().parents[3];OUT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'src'))
from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch
from aidc.model._baseline import build_baseline_outcome

def clean(x):
    if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple,np.ndarray)):return [clean(v) for v in x]
    if isinstance(x,np.integer):return int(x)
    if isinstance(x,(float,np.floating)):return float(x) if np.isfinite(x) else None
    if isinstance(x,np.bool_):return bool(x)
    return x

def run_backend(model,c,start,budget):
    if '--public' in sys.argv:
        began=perf_counter();result=dispatch._run_milp(model,c,budget,.01)
        return result.x,{'wall_s':perf_counter()-began,'status':str(result.status),'primal':result.fun,'dual':getattr(result,'mip_dual_bound',None),'gap':getattr(result,'mip_gap',None),'nodes':getattr(result,'mip_node_count',None)}
    constraint=model.constraint();A=constraint.A
    lp=h.HighsLp();lp.num_col_=len(c);lp.num_row_=A.shape[0]
    lp.col_cost_=c;lp.col_lower_=np.array(model.lower);lp.col_upper_=np.array(model.upper)
    lp.row_lower_=constraint.lb;lp.row_upper_=constraint.ub
    lp.a_matrix_.num_col_=len(c);lp.a_matrix_.num_row_=A.shape[0];lp.a_matrix_.format_=h.MatrixFormat.kColwise
    lp.a_matrix_.start_=A.indptr;lp.a_matrix_.index_=A.indices;lp.a_matrix_.value_=A.data
    lp.integrality_=[h.HighsVarType(i) for i in model.integrality]
    backend=h._Highs()
    for key,value in {'threads':1,'random_seed':20260816,'time_limit':float(budget),'mip_rel_gap':.01,'presolve':'on','output_flag':False}.items():
        assert backend.setOptionValue(key,value)==h.HighsStatus.kOk,(key,value)
    assert backend.passModel(lp)==h.HighsStatus.kOk
    warm=h.HighsSolution();warm.col_value=start;warm.value_valid=True
    assert backend.setSolution(warm)==h.HighsStatus.kOk
    began=perf_counter();backend.run();elapsed=perf_counter()-began
    info=backend.getInfo();solution=backend.getSolution();status=backend.getModelStatus()
    x=np.asarray(solution.col_value) if solution.value_valid else None
    return x,{'wall_s':elapsed,'status':str(status),'primal':info.objective_function_value,'dual':info.mip_dual_bound,'gap':info.mip_gap,'nodes':info.mip_node_count}

started=perf_counter()
cfg=load_config_bundle(ROOT/'configs');cfg['region']=deepcopy(cfg['regions']['R-NW'])
if '--diagnose' in sys.argv:cfg['system']['solver']['verbose']=True
ts=pd.read_parquet(ROOT/'outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet')
tasks=pd.read_csv(ROOT/'outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv')
sizing=next(a['sizing_fixed'] for a in cfg['experiments']['arms'] if a['id']=='A3')
base=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-base',20260816)
baseline=build_baseline_outcome(base);assert baseline.status=='feasible'
original=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-cost',20260816)
assert original.variable_names==base.variable_names
seed_x=baseline.x.copy()
saved=OUT/'cutoff_solution.npz'
if saved.exists():
    probe=np.load(saved)['x']
    if len(probe)==len(seed_x):seed_x=probe.copy()
obj=original.objective_components['primary']+original.objective_components['auxiliary']
oc=original.constraint();oval=oc.A@seed_x
lo,hi=np.asarray(original.lower),np.asarray(original.upper)
assert max(np.maximum(lo-seed_x,seed_x-hi).max(initial=0),np.maximum(oc.lb-oval,oval-oc.ub).max(initial=0))<1e-6
positive,negative=obj>0,obj<0
LB=float(obj[positive]@lo[positive]+obj[negative]@hi[negative]);UB=float(obj@seed_x)
late=np.flatnonzero(original.objective_components['late_fraction']>0)
assert np.all(LB+obj[late]*(1-lo[late])>UB+1e-6)
groups={}
for info in original.meta['task_info']:groups.setdefault((info['gpu'],info['duration']),[]).append(info)
start_set={int(i) for info in original.meta['task_info'] for i in info['indices']}
keep=[i for i in range(len(obj)) if i not in start_set]
mapping={old:new for new,old in enumerate(keep)}
model=dispatch.DispatchModel()
model.variable_names=[original.variable_names[i] for i in keep]
model.lower=lo[keep].tolist();model.upper=hi[keep].tolist()
model.integrality=np.asarray(original.integrality)[keep].tolist()
model.meta=original.meta.copy()
group_data=[];start_to_count={};warm=seed_x[keep].tolist()
for gid,infos in enumerate(groups.values()):
    times=sorted({int(t) for info in infos for t in info['starts'] if t+info['duration']<=info['row']['deadline_h']})
    indices=model.variable(f'counts_{gid}',len(times),upper=len(infos),integer=True)
    by_time=dict(zip(times,indices))
    for info in infos:
        for old,t in zip(info['indices'],info['starts']):
            if t+info['duration']<=info['row']['deadline_h']:start_to_count[int(old)]=int(by_time[int(t)])
    counts=np.zeros(len(times))
    for info in infos:
        selected=int(info['starts'][np.argmax(seed_x[info['indices']])]);counts[times.index(selected)]+=1
    warm.extend(counts.tolist())
    model.equal(f'group_total_{gid}',[(i,1) for i in indices],len(infos))
    group_data.append({'gid':gid,'infos':infos,'times':np.array(times),'indices':indices,'cuts':set()})

for name,row,rlo,rhi in zip(original.row_names,original.rows,original.row_lower,original.row_upper):
    if name.startswith('task_once_'):continue
    terms={}
    for i,value in row.items():
        if i in mapping:terms[mapping[i]]=value
        elif i in start_to_count:
            j=start_to_count[i]
            if j in terms:assert terms[j]==value,(name,j,terms[j],value)
            terms[j]=value
        else:assert i in start_set
    model.row(name,terms,rlo,rhi)
c=np.r_[obj[keep],np.zeros(len(model.variable_names)-len(keep))]
if '--primary-cut' in sys.argv:
    av=original.objective_components['auxiliary']
    aux_lb=float(av[av>0]@lo[av>0]+av[av<0]@hi[av<0])
    pc=np.r_[original.objective_components['primary'][keep],np.zeros(len(model.variable_names)-len(keep))]
    model.row('verified_incumbent_primary_cutoff',enumerate(pc),upper=UB-aux_lb+1e-6)
else:
    model.row('verified_incumbent_combined_cutoff',enumerate(c),upper=UB+1e-6)

def add_cut(group,a,b,need):
    pair=(int(a),int(b));times=group['times'];indices=group['indices']
    if pair in group['cuts']:return False
    group['cuts'].add(pair)
    model.row(f'hall_{group["gid"]}_{a}_{b}',[(int(j),1) for j,t in zip(indices,times) if a<=t<=b],lower=int(need))
    return True

for g in group_data:
    infos=g['infos'];arr=np.array([x['row']['arrive_h'] for x in infos],int);end=np.array([x['row']['deadline_h']-x['duration'] for x in infos],int)
    g['arr'],g['end']=arr,end
    if '--full' not in sys.argv:
        for a in np.unique(arr):add_cut(g,a,167,int(np.sum(arr>=a)))
        for b in np.unique(end):add_cut(g,0,b,int(np.sum(end<=b)))
        for a,b in set(zip(arr,end)):add_cut(g,a,b,int(np.sum((arr>=a)&(end<=b))))
    else:
        prefix=model.variable(f'prefix_{g["gid"]}',len(ts)+1,upper=len(infos))
        model.fix(prefix[0],0);model.fix(prefix[-1],len(infos))
        bytime=dict(zip(g['times'],g['indices']))
        seed_counts=np.array([warm[bytime[t]] if t in bytime else 0 for t in range(len(ts))])
        warm.extend(np.r_[0,np.cumsum(seed_counts)].tolist())
        for t in range(len(ts)):
            terms=[(prefix[t+1],1),(prefix[t],-1)]
            if t in bytime:terms.append((bytime[t],-1))
            model.equal(f'prefix_link_{g["gid"]}_{t}',terms)
        hist=np.zeros((len(ts),len(ts)),dtype=int)
        for a,b in zip(arr,end):hist[a,b]+=1
        K=np.cumsum(np.cumsum(hist[::-1,:],axis=0)[::-1,:],axis=1)
        for a in np.unique(arr):
            for b in np.unique(end):
                if a>b:continue
                need=K[a,b]
                if need<=0:continue
                if a+1<len(ts) and K[a+1,b]==need:continue
                if b>0 and K[a,b-1]==need:continue
                model.row(f'full_hall_{g["gid"]}_{a}_{b}',[(prefix[b+1],1),(prefix[a],-1)],lower=int(need))
c=np.pad(c,(0,len(model.variable_names)-len(c)))

report={'source_hash':hashlib.sha256((ROOT/'src/aidc/model/dispatch.py').read_bytes()).hexdigest(),'original_variables':len(obj),'original_binary':int(np.count_nonzero(original.integrality)),'n_variables':len(c),'n_integer':int(np.count_nonzero(model.integrality)),'initial_rows':len(model.rows),'groups':len(groups),'interval_LB':LB,'feasible_UB':UB,'preparation_s':perf_counter()-started,'iterations':[]}
print(json.dumps(clean(report)),flush=True)
solver_started=perf_counter();best_original=seed_x.copy();best_cost=UB
for iteration in range(10):
    remaining=(5 if '--diagnose' in sys.argv else 60)-(perf_counter()-solver_started)
    if remaining<=.05:break
    candidate,record=run_backend(model,c,np.asarray(warm),remaining if '--full' in sys.argv else min(15.,remaining))
    record['iteration']=iteration;report['iterations'].append(record)
    print(json.dumps(clean(record)),flush=True)
    if candidate is None:break
    violations=[]
    for g in group_data:
        n=np.rint(candidate[g['indices']]).astype(int)
        assert np.max(np.abs(n-candidate[g['indices']]))<1e-5
        A,B=np.unique(g['arr']),np.unique(g['end'])
        contained=((g['arr'][:,None,None]>=A[None,:,None])&(g['end'][:,None,None]<=B[None,None,:])).sum(axis=0)
        prefix=np.r_[0,np.cumsum(n)]
        capacity=prefix[np.searchsorted(g['times'],B,side='right')][None,:]-prefix[np.searchsorted(g['times'],A,side='left')][:,None]
        rows,cols=np.where(contained>capacity)
        for ia,ib in zip(rows,cols):
            a,b=int(A[ia]),int(B[ib])
            if a<=b:violations.append((int(contained[ia,ib]-capacity[ia,ib]),g,a,b,int(contained[ia,ib])))
    record['violations']=len(violations)
    if violations:
        record['cuts_added']=sum(add_cut(g,a,b,need) for _,g,a,b,need in sorted(violations,key=lambda v:-v[0])[:300])
        assert record['cuts_added']>0
        continue
    restored=np.zeros(len(obj));restored[keep]=candidate[:len(keep)]
    for g in group_data:
        slots=np.repeat(g['times'],np.rint(candidate[g['indices']]).astype(int))
        allowed=(g['arr'][:,None]<=slots[None,:])&(slots[None,:]<=g['end'][:,None])
        rr,cc=linear_sum_assignment(np.where(allowed,0.,1e9));assert allowed[rr,cc].all()
        for i,j in zip(rr,cc):
            info=g['infos'][i];old=info['indices'][np.flatnonzero(info['starts']==slots[j])[0]];restored[old]=1.
    values=oc.A@restored
    errors={'bounds':float(np.maximum(lo-restored,restored-hi).max(initial=0)),'rows':float(np.maximum(oc.lb-values,values-oc.ub).max(initial=0)),'integer':float(np.abs(restored[np.array(original.integrality,dtype=bool)]-np.rint(restored[np.array(original.integrality,dtype=bool)])).max(initial=0)),'objective':abs(float(obj@restored)-float(c@candidate))}
    assert max(errors.values())<1e-6,errors
    record['original_restoration']=errors;record['valid_original_objective']=float(obj@restored)
    best_original=restored;best_cost=float(obj@restored);warm=candidate.copy()
    record['valid_global_gap']=(best_cost-record['dual'])/abs(best_cost)
    if record['valid_global_gap']<=.01:break
report['total_s']=perf_counter()-started;report['best_original_objective']=best_cost
report['solver_total_s']=perf_counter()-solver_started;report['final_rows']=len(model.rows)
label='full_hall_public' if '--full' in sys.argv and '--public' in sys.argv else 'full_hall' if '--full' in sys.argv else 'hall'
if '--diagnose' in sys.argv:label+='-diagnostic'
if '--primary-cut' in sys.argv:label+='-primary-cut'
np.savez_compressed(OUT/f'{label}_solution.npz',x=best_original)
(OUT/f'{label}_result.json').write_text(json.dumps(clean(report),indent=2),encoding='utf8')
print(json.dumps(clean(report),indent=2),flush=True)
