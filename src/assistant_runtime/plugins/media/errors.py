"""Stable, sanitized errors. Provider response bodies never cross this boundary."""

import re

from assistant_runtime.api.tool_calls import ToolFailure, ToolOutcome


# These owner operations only observe data; copy/apply/resume/batch/transfer are absent.
READ_ACTIONS = frozenset(
    {
        "status",
        "capabilities",
        "library",
        "asset",
        "search",
        "search_history",
        "search_sources",
        "inspect",
        "files",
        "get",
        "list",
        "batch_get",
        "operation_outcomes",
    }
)
TRANSIENT = frozenset(
    {"provider_network_error", "provider_unavailable", "rate_limited", "openlist_unavailable"}
)
MESSAGES = {
    "provider_file_identity_mismatch": "网盘返回的文件身份或目录归属不符合当前请求，已停止使用该结果。",
    "provider_invalid_response": "网盘接口返回了不符合协议的数据，未能取得可靠结果。",
    "provider_incomplete_listing": "网盘返回的目录清单不完整，未能完成核验。",
    "auth_required": "网盘鉴权失效，需要更新对应账号凭据后再继续。",
    "account_mismatch": "当前网盘账号与原绑定不一致，需要操作者核对账号。",
    "share_cancelled": "分享已取消，需更换有效分享。",
    "share_expired": "分享已过期，需更换有效分享。",
    "share_password_invalid": "分享提取码无效，需核对提取码。",
    "library_content_changed": "删除后的对象身份或内容核验不一致，系统已保留回执并停止后续步骤。",
    "library_write_unconfirmed": "网盘已接受操作，但当前证据不足以确认结果。",
    "directory_copy_receipt_unconfirmed": "整目录转存已提交，但尚未取得可信的目标目录回执；不会重新提交。",
    "directory_copy_unconfirmed": "目标目录与源目录清单尚不一致，系统保留回执等待核验。",
    "directory_restore_unconfirmed": "恢复后的目录清单尚未通过核验。",
    "directory_content_unverifiable": "目录清单不完整，无法安全处理整个目录。",
    "unsubmitted_steps_remain": "已提交步骤已核对，仍有未执行步骤；系统没有擅自继续删除。",
    "provider_network_error": "访问网盘时连接失败或超时。",
    "provider_unavailable": "网盘服务暂时不可用。",
    "rate_limited": "网盘服务暂时限流，请稍后再读取。",
}


class MediaError(RuntimeError):
    def __init__(self, code, *, uncertain=False, field=None, evidence=None):
        self.code, self.uncertain, self.field, self.evidence = code, uncertain, field, evidence
        super().__init__(code)

    def tool_failure(self, action):
        code = (
            self.code
            if isinstance(self.code, str) and re.fullmatch(r"[a-z][a-z0-9_]{1,79}", self.code)
            else "media_error"
        )
        read = action in READ_ACTIONS
        unknown = self.uncertain or not read
        retryable = read and not unknown and code in TRANSIENT
        next_action = (
            "inspect_operation"
            if unknown or not read
            else "retry_read"
            if retryable
            else "update_credentials"
            if code in {"auth_required", "account_mismatch"}
            else "replace_share"
            if code in {"share_cancelled", "share_expired"}
            else "correct_arguments"
            if code == "share_password_invalid"
            else "inspect_provider_response"
        )
        return ToolFailure(
            ToolOutcome(
                code,
                "return",
                "unknown" if unknown else "dispatched",
                "unknown" if unknown else "failed",
                MESSAGES.get(code, "媒体操作未完成：" + code),
                next_action=next_action,
                retryable=retryable,
                field=self.field,
            )
        )
