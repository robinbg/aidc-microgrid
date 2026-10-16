"""Exact full-horizon feasible incumbent cutoff experiment; no production edits."""
from pathlib import Path
from copy import deepcopy
from time import perf_counter
import hashlib,json,sys
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[3]
OUT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'src'))
from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch
from aidc.model._baseline import build_baseline_outcome
from scipy.optimize import linear_sum_assignment

def clean(x):
    if isinstance(x,dict):return {str(k):clean(v) for k,v in x.items()}
    if isinstance(x,(list,tuple,np.ndarray)):return [clean(v) for v in x]
    if isinstance(x,(np.integer,)):return int(x)
    if isinstance(x,(float,np.floating)):return float(x) if np.isfinite(x) else None
    if isinstance(x,np.bool_):return bool(x)
    return x

cfg=load_config_bundle(ROOT/'configs');cfg['region']=deepcopy(cfg['regions']['R-NW'])
ts=pd.read_parquet(ROOT/'outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet')
tasks=pd.read_csv(ROOT/'outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv')
sizing=next(a['sizing_fixed'] for a in cfg['experiments']['arms'] if a['id']=='A3')
started=perf_counter()
base=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-base',20260816)
candidate=build_baseline_outcome(base)
assert candidate.status=='feasible'
model=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-cost',20260816)
assert base.variable_names==model.variable_names
x=candidate.x
if '--fixed' in sys.argv:
    x=np.load(OUT/'fixed_tasks_solution.npz')['x']
matrix=model.constraint().A
ax=matrix@x
bound_error=float(np.maximum(np.asarray(model.lower)-x,x-np.asarray(model.upper)).max(initial=0))
row_error=float(np.maximum(np.asarray(model.row_lower)-ax,ax-np.asarray(model.row_upper)).max(initial=0))
integer=np.asarray(model.integrality,dtype=bool)
int_error=float(np.abs(x[integer]-np.rint(x[integer])).max(initial=0))
assert max(bound_error,row_error,int_error)<1e-7
primary=model.objective_components['primary']
auxiliary=model.objective_components['auxiliary']
combined=primary+auxiliary
cutoff=float(combined@x)
original_n=len(x)
original_lower=np.asarray(model.lower).copy()
original_upper=np.asarray(model.upper).copy()
original_integrality=np.asarray(model.integrality,dtype=bool).copy()
original_constraint=model.constraint()
cut_vector=combined
cut_upper=cutoff
if '--primary-cut' in sys.argv:
    cut_vector=primary
    cut_upper-=float(auxiliary[auxiliary>0]@np.asarray(model.lower)[auxiliary>0]+auxiliary[auxiliary<0]@np.asarray(model.upper)[auxiliary<0])
model.row('verified_incumbent_cutoff',list(enumerate(cut_vector)),upper=cut_upper+1e-6)
report={'hours':len(ts),'tasks':len(tasks),'n_variables':len(model.variable_names),'binary_count':int(integer.sum()),'input_hashes':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ROOT/'src/aidc/model/dispatch.py',ROOT/'src/aidc/model/_baseline.py',ROOT/'outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet',ROOT/'outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv']},'incumbent':{'bound_error':bound_error,'row_error':row_error,'integrality_error':int_error,'primary':cutoff,'sla_penalty':float(model.objective_components['sla_penalty']@x)},'preparation_s':perf_counter()-started}
prefix='cutoff'
if '--fixed' in sys.argv:prefix='fixed_incumbent_cutoff'
if '--tighten' in sys.argv or '--order' in sys.argv or '--aggregate' in sys.argv:
    prefix='tighten'
    lower,upper=np.asarray(model.lower),np.asarray(model.upper)
    positive,negative=combined>0,combined<0
    objective_lower=float(combined[positive]@lower[positive]+combined[negative]@upper[negative])
    fixes=integer & (combined>0) & (lower<1) & (objective_lower+combined*(1-lower)>cutoff+1e-6)
    for j in np.flatnonzero(fixes):model.upper[j]=0.
    report['objective_bound_proof']={'interval_lower_bound':objective_lower,'cutoff':cutoff,'n_binary_fixed_zero':int(fixes.sum()),'minimum_forbidden_value':float(np.min(objective_lower+combined[fixes]*(1-lower[fixes]),initial=np.inf))}
if '--order' in sys.argv:
    prefix='order'
    # Only apply after all possible late starts are proved dominated. Equal
    # GPU/duration tasks with ordered release/deadline windows can be uncrossed
    # without altering any hourly load, physical decision or primary objective.
    late=model.objective_components['late_fraction']>0
    assert np.all(np.asarray(model.upper)[late]==0)
    groups={}
    for info in model.meta['task_info']:
        groups.setdefault((info['gpu'],info['duration']),[]).append(info)
    ordering=[]
    for infos in groups.values():
        chains=[]
        for info in sorted(infos,key=lambda z:(z['row']['arrive_h'],z['row']['deadline_h'],str(z['row']['task_id']))):
            options=[(j,c[-1]['row']['deadline_h']) for j,c in enumerate(chains) if c[-1]['row']['deadline_h']<=info['row']['deadline_h']]
            if not options:chains.append([info]);continue
            j=max(options,key=lambda z:z[1])[0]
            previous=chains[j][-1]
            model.row(f'ordered_equal_work_{len(ordering)}',[(idx,float(t)/len(ts)) for idx,t in zip(previous['indices'],previous['starts'])]+[(idx,-float(t)/len(ts)) for idx,t in zip(info['indices'],info['starts'])],upper=0)
            ordering.append((previous['row']['task_id'],info['row']['task_id']))
            chains[j].append(info)
    report['ordered_workload_cuts']=len(ordering)
aggregate_groups=[]
if '--aggregate' in sys.argv:
    prefix='aggregate'
    assert np.all(np.asarray(model.upper)[model.objective_components['late_fraction']>0]==0)
    groups={}
    for info in model.meta['task_info']:
        groups.setdefault((info['gpu'],info['duration']),[]).append(info)
    for group_id,infos in enumerate(groups.values()):
        by_time={}
        for info in infos:
            for idx,t in zip(info['indices'],info['starts']):
                if model.upper[idx]>0:
                    by_time.setdefault(int(t),[]).append(int(idx))
                    model.integrality[idx]=0
        times=sorted(by_time)
        counts=model.variable(f'aggregate_task_count_{group_id}',len(times),upper=len(infos),integer=True)
        for t,j in zip(times,counts):
            model.equal(f'aggregate_task_count_{group_id}_{t}',[(j,1)]+[(idx,-1) for idx in by_time[t]])
        aggregate_groups.append((infos,times,counts))
    added=len(model.variable_names)-len(primary)
    model.objective_components={k:np.pad(v,(0,added)) for k,v in model.objective_components.items()}
    primary=model.objective_components['primary'];auxiliary=model.objective_components['auxiliary']
    report['aggregate_formulation']={'groups':len(groups),'count_variables':added,'integer_variables':int(np.count_nonzero(model.integrality)),'n_variables':len(model.variable_names),'n_rows':len(model.rows)}
if '--lp' in sys.argv:
    prefix='lp_relaxation_diagnostic'
    model.integrality=[0]*len(model.integrality)
    report['diagnostic_only']='LP relaxation for bound/fractional-variable diagnosis, never accepted as a dispatch'
print(json.dumps(report),flush=True)
t=perf_counter()
budget=max(.01,35.-(perf_counter()-started)-1.) if '--short' in sys.argv else 60.
report['requested_solver_time_s']=budget
result=dispatch._run_milp(model,primary+auxiliary,budget,.01)
report['solve_wall_s']=perf_counter()-t
report['solver']=dispatch._stage_record(result,'cutoff_full168',.01)
if result.x is not None:
    if aggregate_groups:
        before_objective=float((primary+auxiliary)@result.x)
        for infos,times,counts in aggregate_groups:
            n=np.rint(result.x[counts]).astype(int)
            assert np.max(np.abs(result.x[counts]-n))<1e-5
            slots=np.repeat(times,n)
            assert len(slots)==len(infos)
            allowed=np.asarray([[float(info['row']['arrive_h'])<=t and t+info['duration']<=float(info['row']['deadline_h']) for t in slots] for info in infos])
            rr,cc=linear_sum_assignment(np.where(allowed,0.,1e9))
            assert allowed[rr,cc].all()
            for i,j in zip(rr,cc):
                info=infos[i]
                result.x[info['indices']]=0.
                result.x[info['indices'][np.flatnonzero(info['starts']==slots[j])[0]]]=1.
        restored=result.x[:original_n]
        ax=original_constraint.A@restored
        restore_bound=float(np.maximum(original_lower-restored,restored-original_upper).max(initial=0))
        restore_rows=float(np.maximum(original_constraint.lb-ax,ax-original_constraint.ub).max(initial=0))
        restore_integer=float(np.abs(restored[original_integrality]-np.rint(restored[original_integrality])).max(initial=0))
        after_objective=float((primary+auxiliary)@result.x)
        report['integer_restoration']={'original_bound_error':restore_bound,'original_row_error':restore_rows,'original_integrality_error':restore_integer,'objective_before':before_objective,'objective_after':after_objective,'objective_difference':after_objective-before_objective}
        assert max(restore_bound,restore_rows,restore_integer)<1e-6
        assert abs(after_objective-before_objective)<1e-6
    report['primary_value']=float(primary@result.x)
    report['auxiliary_value']=float(auxiliary@result.x)
    report['sla_penalty']=float(model.objective_components['sla_penalty']@result.x)
    if '--lp' not in sys.argv:
        extracted=dispatch._extract_result(model,result,'optimal' if result.status==0 else 'feasible',perf_counter()-started,[report['solver']])
        extracted.trajectories.to_parquet(OUT/f'{prefix}_trajectories.parquet')
        extracted.task_schedule.to_csv(OUT/f'{prefix}_tasks.csv',index=False)
    else:
        fractional={}
        for i,name in enumerate(model.variable_names):
            if original_integrality[i] and abs(result.x[i]-round(result.x[i]))>1e-6:
                group='start' if name.startswith('start_') else name.split('[')[0]
                fractional[group]=fractional.get(group,0)+1
        report['fractional_original_binary_by_family']=fractional
    np.savez_compressed(OUT/f'{prefix}_solution.npz',x=result.x)
report['total_wall_s']=perf_counter()-started
(OUT/f'{prefix}_result.json').write_text(json.dumps(clean(report),indent=2),encoding='utf8')
print(json.dumps(clean(report),indent=2),flush=True)
