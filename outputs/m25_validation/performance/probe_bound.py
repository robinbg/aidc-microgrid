"""Certified lower bound only: relaxed job timing, full physical MILP."""
from pathlib import Path
from copy import deepcopy
from time import perf_counter
import hashlib,json,sys
import numpy as np
import pandas as pd
ROOT=Path(__file__).resolve().parents[3];OUT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'src'))
from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch
from probe_fixed_tasks import clean,errors
began=perf_counter()
cfg=load_config_bundle(ROOT/'configs');cfg['region']=deepcopy(cfg['regions']['R-NW'])
ts=pd.read_parquet(ROOT/'outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet')
tasks=pd.read_csv(ROOT/'outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv')
sizing=next(a['sizing_fixed'] for a in cfg['experiments']['arms'] if a['id']=='A3')
original=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-cost',20260816)
seed_path=OUT/'fresh_fixed_tasks_solution.npz'
if not seed_path.exists():seed_path=OUT/'fixed_tasks_refined_solution.npz'
x=np.load(seed_path)['x'];assert max(errors(original,x).values())<1e-6
start_set={int(i) for info in original.meta['task_info'] for i in info['indices']}
keep=[i for i in range(len(original.variable_names)) if i not in start_set]
mapold={old:new for new,old in enumerate(keep)}
model=dispatch.DispatchModel();model.meta=original.meta.copy()
model.variable_names=[original.variable_names[i] for i in keep]
model.lower=np.asarray(original.lower)[keep].tolist();model.upper=np.asarray(original.upper)[keep].tolist()
model.integrality=np.asarray(original.integrality)[keep].tolist()
for name,row,lo,hi in zip(original.row_names,original.rows,original.row_lower,original.row_upper):
    if name.startswith(('task_once_','IT_demand_','GPU_capacity_')):continue
    assert not any(i in start_set for i in row),name
    model.row(name,[(mapold[i],value) for i,value in row.items()],lo,hi)
its=[mapold[i] for i in original.meta['variables']['IT_load']]
base=original.meta['base'];pc=original.meta['p_card'];T=len(ts)
for t,i in enumerate(its):model.lower[i]=float(base[t])
prefix=model.variable('relaxed_processed_task_energy',T+1,upper=float((tasks.gpu*tasks.duration_h).sum()*pc))
model.fix(prefix[0],0.)
for t in range(T):model.equal(f'processed_link_{t}',[(prefix[t+1],1),(prefix[t],-1),(its[t],-1)],-float(base[t]))
gpu=tasks.gpu.to_numpy()*pc;dur=tasks.duration_h.to_numpy();arr=tasks.arrive_h.to_numpy();latest=tasks.latest_h.to_numpy()
total=float(gpu@dur);model.fix(prefix[-1],total)
for t in range(T+1):
    upper=float(gpu@np.minimum(dur,np.maximum(0,t-arr)))
    lower=float(gpu@np.maximum(0,dur-np.maximum(0,latest-t)))
    model.row(f'relaxed_cumulative_window_{t}',[(prefix[t],1)],lower,upper)
if '--intervals' in sys.argv:
    for a in range(T):
        for b in range(a+1,T+1):
            required=float(gpu@np.maximum(0,dur-np.maximum(0,a-arr)-np.maximum(0,latest-b)))
            if required>0:model.row(f'relaxed_interval_{a}_{b}',[(prefix[b],1),(prefix[a],-1)],lower=required)
physical_obj=original.objective_components['operating_cost']+original.objective_components['auxiliary']
c=np.r_[physical_obj[keep],np.zeros(T+1)]
candidate=np.r_[x[keep],np.r_[0,np.cumsum(x[original.meta['variables']['IT_load']]-base)]]
constraints=model.constraint();values=constraints.A@candidate
candidate_errors={'bound':float(np.maximum(np.asarray(model.lower)-candidate,candidate-np.asarray(model.upper)).max(initial=0)),'row':float(np.maximum(constraints.lb-values,values-constraints.ub).max(initial=0))}
assert max(candidate_errors.values())<1e-6,candidate_errors
UB=float((original.objective_components['primary']+original.objective_components['auxiliary'])@x)
report={'scope':'Lower-bound relaxation only; its trajectory is not an accepted dispatch. All physical binaries retained. Nonnegative SLA penalty omitted. Original job windows relaxed to necessary cumulative energy inequalities.','original_variables':len(original.variable_names),'n_variables':len(c),'n_binary':int(np.count_nonzero(model.integrality)),'n_rows':len(model.rows),'candidate_original_upper_bound':UB,'candidate_relaxation_inclusion':candidate_errors,'seed_path':str(seed_path.relative_to(ROOT)),'source_sha256':hashlib.sha256((ROOT/'src/aidc/model/dispatch.py').read_bytes()).hexdigest(),'preparation_s':perf_counter()-began}
print(json.dumps(clean(report)),flush=True)
t=perf_counter();result=dispatch._run_milp(model,c,25.,.001)
report['solver_s']=perf_counter()-t;report['solver']=dispatch._stage_record(result,'physical_job_timing_relaxation',.001)
dual=getattr(result,'mip_dual_bound',None)
if dual is not None and np.isfinite(dual):report['certified_original_global_gap']=max(0.,(UB-float(dual))/abs(UB))
report['total_s']=perf_counter()-began
label='physical_bound_intervals' if '--intervals' in sys.argv else 'physical_bound'
(OUT/f'{label}_result.json').write_text(json.dumps(clean(report),indent=2),encoding='utf8')
print(json.dumps(clean(report),indent=2),flush=True)
