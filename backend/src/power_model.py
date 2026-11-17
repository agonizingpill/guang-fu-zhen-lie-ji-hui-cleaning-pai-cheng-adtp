import json,hashlib
from functools import lru_cache
from pathlib import Path
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split
from sklearn.metrics import mean_absolute_error
FEATURES=['capacity_kw','irradiance','module_temp','slope']
# 标定适用范围：超出任一维度即拒评/转人工复核，不外推模型。
CALIBRATION={'capacity_kw':(80,150),'irradiance':(350,1050),'module_temp':(18,65),'slope':(10,35)}
LABELS={'capacity_kw':'装机kW','irradiance':'辐照W/m²','module_temp':'组件温度°C','slope':'坡度°'}
@lru_cache(maxsize=1)
def train_reference():
    source=Path(__file__).resolve().parents[1]/'fixtures/clean_reference.json'
    raw=source.read_bytes();rows=json.loads(raw)
    x=np.array([[r[k] for k in FEATURES] for r in rows]);y=np.array([r['clean_kw'] for r in rows])
    train_x,test_x,train_y,test_y=train_test_split(x,y,test_size=.2,random_state=42)
    model=RandomForestRegressor(n_estimators=100,max_depth=8,min_samples_leaf=2,random_state=42,n_jobs=1)
    model.fit(train_x,train_y)
    evidence={'reference_rows':len(rows),'train_rows':len(train_x),'test_rows':len(test_x),'train_mae_kw':float(mean_absolute_error(train_y,model.predict(train_x))),'holdout_mae_kw':float(mean_absolute_error(test_y,model.predict(test_x))),'feature_importance':dict(zip(FEATURES,model.feature_importances_.tolist())),'random_state':42,'sha256':hashlib.sha256(raw).hexdigest(),'method':'RandomForestRegressor / 100 trees / max_depth 8'}
    return model,evidence

def estimate(survey):
    if survey.irradiance<350:raise ValueError('辐照低于350 W/m²，请在稳定光照下补测')
    model,evidence=train_reference()
    clean=max(0,float(model.predict([[getattr(survey,k) for k in FEATURES]])[0]))
    loss=max(0,clean-survey.observed_kw)
    return clean,loss,evidence

def calibration_gaps(conditions,tag):
    # conditions: dict with FEATURES keys; returns human-readable out-of-range items.
    return [f'{tag}{LABELS[k]}{conditions[k]}不在{lo}–{hi}标定范围' for k,(lo,hi) in CALIBRATION.items() if not lo<=conditions[k]<=hi]

def predict_clean(conditions):
    # 同一清洁功率模型在任意给定条件下的预测，用于把前后读数换算到同一条件。
    model,evidence=train_reference()
    return max(0,float(model.predict([[conditions[k] for k in FEATURES]])[0])),evidence
