import os,time,json,traceback
from pathlib import Path
from sqlalchemy import select
import redis
from .database import Session
from .models import ArrayBlock,SoilingSurvey
from .power_model import estimate
from . import review_service
queue=redis.Redis.from_url(os.getenv('REDIS_URL','redis://redis:6379/0'),decode_responses=True)

def assess(key):
    with Session() as db:
        survey=db.scalar(select(SoilingSurvey).where(SoilingSurvey.id==key).with_for_update())
        if not survey or survey.state!='evaluating':return
        block=db.get(ArrayBlock,survey.block_id)
        clean,loss,evidence=estimate(survey)
        survey.clean_kw=clean;survey.predicted_loss=loss;survey.model_evidence=evidence
        survey.recommended_water=round(block.area_m2*.6*(1+.1*survey.dust),1)
        survey.state='assessed';db.commit()
        Path('/artifacts').mkdir(exist_ok=True)
        Path(f'/artifacts/survey-{key}.json').write_text(json.dumps({'survey_id':key,'clean_kw':clean,'loss_kw':loss,'model':evidence}))

def assess_failed(key,error):
    with Session() as db:
        survey=db.get(SoilingSurvey,key)
        if survey:survey.state='blocked';survey.model_evidence={'error':str(error)};db.commit()

def review(payload):
    rid,_,ver=payload.partition(':')
    with Session() as db:
        done=review_service.apply_computation(db,int(rid),int(ver))
        if not done:return
        Path('/artifacts').mkdir(exist_ok=True)
        Path(f'/artifacts/review-{rid}-v{done.version}.json').write_text(json.dumps({'review_id':rid,'state':done.state,'normalized_gain':done.normalized_gain,'block_reason':done.block_reason,'basis':done.basis}))

def review_failed(payload,error):
    rid,_,ver=payload.partition(':')
    with Session() as db:review_service.fail(db,int(rid),int(ver),error)

# 启动即恢复待办：中断时回滚仍处待核定的复核重新入队，只重算不新增结算。
with Session() as db:
    recovered=review_service.recover_pending(db,queue)
    if recovered:print(f'recovered {recovered} pending revenue reviews',flush=True)

while True:
    Path('/tmp/solar-heartbeat').touch()
    job=queue.blpop(['solar:assessment','solar:review'],timeout=3)
    if not job:continue
    source,payload=job
    try:
        if source=='solar:assessment':assess(int(payload))
        else:review(payload)
    except Exception as error:
        traceback.print_exc()
        try:
            if source=='solar:assessment':assess_failed(int(payload),error)
            else:review_failed(payload,error)
        except Exception:traceback.print_exc()
