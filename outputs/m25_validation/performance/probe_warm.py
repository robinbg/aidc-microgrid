"""Real installed-HiGHS incumbent-start experiment, no production mutation."""
from pathlib import Path
from copy import deepcopy
from time import perf_counter
import hashlib,json,sys
import numpy as np
import pandas as pd
from scipy.optimize._highspy import _core as h
ROOT=Path(__file__).resolve().parents[3];OUT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'src'))
from aidc.data.chemistry import load_config_bundle
from aidc.model import dispatch
from probe_fixed_tasks import errors,clean
started=perf_counter()
cfg=load_config_bundle(ROOT/'configs');cfg['region']=deepcopy(cfg['regions']['R-NW'])
ts=pd.read_parquet(ROOT/'outputs/m2_validation/inputs_normal_168h_r_nw_a3.parquet')
tasks=pd.read_csv(ROOT/'outputs/m2_validation/tasks_normal_168h_r_nw_a3.csv')
sizing=next(a['sizing_fixed'] for a in cfg['experiments']['arms'] if a['id']=='A3')
model=dispatch.build_dispatch_model(ts,tasks,cfg,sizing,'S-cost',20260816)
label='fresh_warm' if '--fresh' in sys.argv else 'refined_warm'
seedpath=OUT/('fresh_fixed_tasks_solution.npz' if '--fresh' in sys.argv else 'fixed_tasks_refined_solution.npz')
x=np.load(seedpath)['x'];residual=errors(model,x);assert max(residual.values())<1e-6,residual
primary=model.objective_components['primary'];aux=model.objective_components['auxiliary'];c=primary+aux
UB=float(c@x);lo=np.asarray(model.lower);hi=np.asarray(model.upper)
auxlb=float(aux[aux>0]@lo[aux>0]+aux[aux<0]@hi[aux<0])
model.row('verified_incumbent_primary_cutoff',enumerate(primary),upper=UB-auxlb+1e-6)
constraint=model.constraint();A=constraint.A
lp=h.HighsLp();lp.num_col_=len(c);lp.num_row_=A.shape[0]
lp.col_cost_=c;lp.col_lower_=lo;lp.col_upper_=hi;lp.row_lower_=constraint.lb;lp.row_upper_=constraint.ub
lp.a_matrix_.num_col_=len(c);lp.a_matrix_.num_row_=A.shape[0];lp.a_matrix_.format_=h.MatrixFormat.kColwise
lp.a_matrix_.start_=A.indptr;lp.a_matrix_.index_=A.indices;lp.a_matrix_.value_=A.data
lp.integrality_=[h.HighsVarType(i) for i in model.integrality]
backend=h._Highs();budget=max(.01,30.-(perf_counter()-started)-1.)
for k,v in {'threads':1,'random_seed':20260816,'time_limit':budget,'mip_rel_gap':.01,'presolve':'on','output_flag':False}.items():assert backend.setOptionValue(k,v)==h.HighsStatus.kOk
assert backend.passModel(lp)==h.HighsStatus.kOk
warm=h.HighsSolution();warm.col_value=x;warm.value_valid=True
assert backend.setSolution(warm)==h.HighsStatus.kOk
report={'mode':'real HiGHS setSolution; entire original 168h MILP','seed_file':str(seedpath.relative_to(ROOT)),'seed_sha256':hashlib.sha256(seedpath.read_bytes()).hexdigest(),'seed_combined_cost':UB,'seed_validation':residual,'preparation_s':perf_counter()-started,'requested_solve_s':budget}
print(json.dumps(clean(report)),flush=True)
t=perf_counter();backend.run();info=backend.getInfo();sol=backend.getSolution()
report.update(solver_wall_s=perf_counter()-t,status=str(backend.getModelStatus()),primal=info.objective_function_value,dual=info.mip_dual_bound,gap=info.mip_gap,nodes=info.mip_node_count,total_wall_s=perf_counter()-started)
if sol.value_valid:
    resultx=np.asarray(sol.col_value);report['original_validation']=errors(model,resultx);assert max(report['original_validation'].values())<1e-6
    np.savez_compressed(OUT/f'{label}_solution.npz',x=resultx)
(OUT/f'{label}_result.json').write_text(json.dumps(clean(report),indent=2),encoding='utf8')
print(json.dumps(clean(report),indent=2),flush=True)
