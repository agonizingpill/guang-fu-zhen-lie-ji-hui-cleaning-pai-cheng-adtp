from datetime import datetime
from sqlalchemy import select
from fastapi import HTTPException
from .models import WashMission,SoilingSurvey,ArrayBlock
from .operations_models import WastewaterRecord,WashSettlement,RevenueReview
from .field_service import require,audit

def prepare(db,b,u):
    m=require(db,WashMission,b.mission_id,True)
    if m.status!='accepted' or not m.verified_gain or m.verified_gain<=0:raise HTTPException(409,'须已验收的正收益清洗任务')
    d=db.scalar(select(WastewaterRecord).where(WastewaterRecord.mission_id==m.id))
    if not d or d.state!='handed_over':raise HTTPException(409,'须完成废水去向交接后结算')
    if db.scalar(select(WashSettlement).where(WashSettlement.mission_id==m.id)):raise HTTPException(409,'任务已有合同结算凭据')
    review=db.scalar(select(RevenueReview).where(RevenueReview.mission_id==m.id).with_for_update())
    if not review or review.state!='settleable':raise HTTPException(409,'收益复核未通过：'+(review.block_reason if review and review.block_reason else '待复核核定，暂不可结算'))
    gain=review.normalized_gain
    s=require(db,SoilingSurvey,m.survey_id);a=require(db,ArrayBlock,s.block_id)
    revenue=round(gain*b.price_per_kw,2);cost=round(m.water_liters*b.cost_per_liter,2);margin=round(revenue-cost,2)
    if margin<0 and len(b.negative_reason.strip())<5:raise HTTPException(409,'负毛利须补充合同履约说明（至少5字）')
    snapshot={'mission_id':m.id,'block_code':a.code,'survey_code':s.code,'verified_gain_kw':gain,'post_temp':review.post_temp,'used_liters':m.water_liters,'accepted_at':m.accepted_at.isoformat() if m.accepted_at else None,'wastewater_id':d.id,'manifest':d.manifest,'recovered_liters':d.recovered_liters,'receipt':d.receipt,'review_id':review.id,'review_version':review.version,'review_basis':review.basis}
    row=WashSettlement(**b.model_dump(),revenue=revenue,water_cost=cost,margin=margin,snapshot=snapshot)
    db.add(row);audit(db,u,f'编制合同结算 任务{m.id} 复核收益{gain}kW 毛利{margin}');db.commit();return row

def sign(db,key,u):
    ref=require(db,WashSettlement,key)
    m=require(db,WashMission,ref.mission_id,True)
    review=db.scalar(select(RevenueReview).where(RevenueReview.mission_id==m.id).with_for_update())
    row=require(db,WashSettlement,key,True)
    if row.state!='draft':raise HTTPException(409,'结算已签记，不可重复或覆盖')
    if not review or review.state!='settleable':raise HTTPException(409,'收益复核未通过，结算冻结中：'+(review.block_reason if review and review.block_reason else '待复核核定'))
    if row.snapshot.get('review_version')!=review.version:raise HTTPException(409,'复测已修订，结算金额待重新核定后再签结')
    row.state='signed';row.signed_by=u.email;row.signed_at=datetime.utcnow();audit(db,u,f'合同结算{key}签结（本地凭据）');db.commit();return row
