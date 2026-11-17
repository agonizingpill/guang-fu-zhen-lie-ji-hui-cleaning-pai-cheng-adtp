from datetime import datetime
from sqlalchemy import select
from fastapi import HTTPException
from .models import WashMission,SoilingSurvey
from .operations_models import RevenueReview,WashSettlement
from .field_service import require,audit
from .power_model import predict_clean,calibration_gaps

QUEUE='solar:review'

def enqueue(queue,review):
    # 载荷携带读数版本：晚到的旧计算凭版本不符被丢弃，不覆盖新读数。
    queue.rpush(QUEUE,f'{review.id}:{review.reading_version}')

def snapshots(survey,mission,post_temp):
    pre={'survey_code':survey.code,'capacity_kw':survey.capacity_kw,'slope':survey.slope,'irradiance':survey.irradiance,'module_temp':survey.module_temp,'observed_kw':survey.observed_kw}
    post={'post_kw':mission.post_kw,'post_irradiance':mission.post_irradiance,'post_temp':post_temp}
    return pre,post

def register_retest(db,mission,survey,post_temp):
    # 复测/修订一经受理：复核回到待核定并递增版本，草稿结算凭版本差立即冻结。
    review=db.scalar(select(RevenueReview).where(RevenueReview.mission_id==mission.id).with_for_update())
    if not review:
        review=RevenueReview(mission_id=mission.id,reading_version=0,version=0);db.add(review)
    pre,post=snapshots(survey,mission,post_temp)
    review.state='pending';review.block_reason='';review.post_temp=post_temp
    review.pre_snapshot=pre;review.post_snapshot=post
    review.basis=None;review.normalized_gain=None;review.decided_by=''
    review.reading_version+=1;review.version+=1
    db.flush();return review

def create(db,b,u,queue):
    m=require(db,WashMission,b.mission_id,True)
    if m.status not in ['retested','accepted'] or m.post_kw is None:raise HTTPException(409,'须先完成清洗后复测，再发起收益复核')
    if db.scalar(select(RevenueReview).where(RevenueReview.mission_id==m.id)):raise HTTPException(409,'任务已有收益复核，修订复测即自动重新核定')
    survey=require(db,SoilingSurvey,m.survey_id)
    review=register_retest(db,m,survey,b.post_temp)
    audit(db,u,f'发起收益复核 任务{m.id} 补录复测温度{b.post_temp}°C');db.commit();enqueue(queue,review);return review

def resolve(db,key,b,u):
    ref=require(db,RevenueReview,key)
    m=require(db,WashMission,ref.mission_id,True)
    review=require(db,RevenueReview,key,True)
    if review.state!='manual':raise HTTPException(409,'仅转人工复核的收益核定可裁定')
    review.state='settleable';review.block_reason='';review.decided_by=u.email
    review.basis={**(review.basis or {}),'manual_decision':{'by':u.email,'at':datetime.utcnow().isoformat(),'note':b.rationale}}
    review.version+=1
    sync_draft_settlement(db,review,m)
    audit(db,u,f'人工裁定收益复核{key} 可结算：{b.rationale}');db.commit();return review

def compute(review):
    # 用清洁功率模型把清洗前后读数换算到同一条件：post_kw × 模型(前条件) ÷ 模型(后条件)。
    pre,post=review.pre_snapshot,review.post_snapshot
    pre_cond={'capacity_kw':pre['capacity_kw'],'irradiance':pre['irradiance'],'module_temp':pre['module_temp'],'slope':pre['slope']}
    post_cond={'capacity_kw':pre['capacity_kw'],'irradiance':post['post_irradiance'],'module_temp':post['post_temp'],'slope':pre['slope']}
    gaps=calibration_gaps(pre_cond,'清洗前')+calibration_gaps(post_cond,'复测')
    clean_pre,evidence=predict_clean(pre_cond);clean_post,_=predict_clean(post_cond)
    factor=clean_pre/clean_post if clean_post>0 else 0
    normalized_post=round(post['post_kw']*factor,4)
    gain=round(normalized_post-pre['observed_kw'],4)
    mae=round(evidence['holdout_mae_kw'],4)
    review.basis={'method':'clean_power_model_ratio','clean_pre_kw':round(clean_pre,4),'clean_post_kw':round(clean_post,4),'factor':round(factor,6),'normalized_post_kw':normalized_post,'gain_kw':gain,'holdout_mae_kw':mae,'evidence_sha256':evidence['sha256'],'computed_at':datetime.utcnow().isoformat()}
    review.normalized_gain=gain
    if gaps:
        review.state='manual';review.block_reason='超出标定范围：'+'；'.join(gaps)
    elif gain<=mae:
        review.state='manual';review.block_reason=f'复核收益{gain}kW未超出模型留出误差{mae}kW，无法区别于预测误差'
    else:
        review.state='settleable';review.block_reason=''

def sync_draft_settlement(db,review,mission):
    # 重新核定后仅更新草稿金额；已签结金额和依据保持不变。
    s=db.scalar(select(WashSettlement).where(WashSettlement.mission_id==review.mission_id).with_for_update())
    if not s or s.state!='draft':return
    s.revenue=round(review.normalized_gain*s.price_per_kw,2)
    s.water_cost=round(mission.water_liters*s.cost_per_liter,2)
    s.margin=round(s.revenue-s.water_cost,2)
    s.snapshot={**s.snapshot,'verified_gain_kw':review.normalized_gain,'post_temp':review.post_temp,'review_id':review.id,'review_version':review.version,'review_basis':review.basis}

def apply_computation(db,review_id,reading_version):
    ref=db.get(RevenueReview,review_id)
    if not ref:return None
    mission=require(db,WashMission,ref.mission_id,True)
    review=db.scalar(select(RevenueReview).where(RevenueReview.id==review_id).with_for_update().execution_options(populate_existing=True))
    if review.state!='pending' or review.reading_version!=reading_version:return None
    compute(review)
    review.version+=1
    if review.state=='settleable':sync_draft_settlement(db,review,mission)
    db.commit();return review

def fail(db,review_id,reading_version,error):
    # 只终结与失败计算同版的待核定复核；修订后的新读数由新任务继续核定。
    review=db.get(RevenueReview,review_id)
    if not review or review.state!='pending' or review.reading_version!=reading_version:return
    review.state='manual';review.block_reason=f'收益核定计算失败，转人工复核：{error}'[:400]
    review.version+=1;db.commit()

def recover_pending(db,queue):
    # 意外中断后恢复待办：重投待核定复核；只重算复核与草稿金额，不新增结算。
    rows=db.execute(select(RevenueReview.id,RevenueReview.reading_version).where(RevenueReview.state=='pending')).all()
    for rid,ver in rows:queue.rpush(QUEUE,f'{rid}:{ver}')
    return len(rows)
