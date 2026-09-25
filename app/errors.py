from enum import IntEnum


class HTTPStatus(IntEnum):
    OK = 200
    CREATED = 201
    BAD_REQUEST = 400
    NOT_FOUND = 404
    CONFLICT = 409


ERROR_NOT_FOUND = {
    "project": "项目不存在",
    "park": "园区不存在",
    "entity": "主体不存在",
    "intent": "合作意向不存在",
    "milestone": "里程碑不存在",
    "approval": "尚未立项",
    "capacity_report": "产能登记不存在",
    "follow_up": "跟进事项不存在",
    "capacity_curve": "产能曲线数据不存在",
    "capacity_pool": "该园区尚未登记此资源类型的容量",
    "reservation": "容量预约不存在",
}

ERROR_DUPLICATE = {
    "project_name": "项目名称已存在",
    "project_code": "项目编号已存在",
    "approval": "该项目已立项，不可重复操作",
    "capacity_report": "该月份的产能报告已存在，请勿重复登记",
}

ERROR_STATUS = {
    "only_attracting_can_submit_intent": "当前项目状态为「{status}」，只有「招商中」的项目才能提交合作意向",
    "only_negotiating_can_approve": "当前项目状态为「{status}」，只有「洽谈中」的项目才能立项",
    "only_established_or_uc_can_add_milestone": "当前项目状态为「{status}」，只有「已立项」或「建设中」的项目才能新增里程碑",
    "only_commissioned_can_report_capacity": "仅已投产项目可登记月度产能",
}

ERROR_OPERATION_FAILED = {
    "approval": "立项失败",
    "capacity_report": "登记失败",
}

ERROR_RESERVATION = {
    "insufficient": "容量不足，预约无法确认",
    "project_park_mismatch": "项目不属于该园区，无法在此园区登记容量预约",
    "pool_missing": "该园区尚未登记「{resource_type}」的容量，无法确认预约",
    "invalid_status": "当前预约状态为「{status}」，不允许执行此操作",
    "terminal_revision": "已释放或已转移的预约不可再修订",
    "period_fully_past": "有效期间已全部成为历史月份，无可确认的额度",
    "release_past_period": "释放生效月份不能早于当月，历史月份的预约不可被覆盖",
    "transfer_park_mismatch": "目标项目不属于同一园区，容量预约不可跨园区转移",
    "transfer_no_remaining": "该预约已无未来月份的有效额度，无可转移内容",
    "transfer_amount_exceeds": "转移量不能超过当前有效额度 {amount}",
    "transfer_self": "不能转移给原项目自身",
}


def fmt(msg: str, **kwargs) -> str:
    return msg.format(**kwargs)
