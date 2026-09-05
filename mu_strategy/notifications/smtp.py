from __future__ import annotations

import re
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from typing import Mapping

from mu_strategy.canonical import canonical_sha256
from mu_strategy.market_data.trusted_data.contracts import Clock, SystemClock
from mu_strategy.notifications.events import AlertEvent, AlertKind, DeliveryState, NotificationError


@dataclass(frozen=True, repr=False)
class SmtpConfig:
    host: str
    sender: str
    recipient: str
    authorization_code: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.host not in {"smtp.126.com", "smtp.163.com", "smtp.yeah.net"}:
            raise NotificationError("MU_SMTP_HOST must be a supported NetEase SMTP host")
        for name, address in (("MU_SMTP_SENDER", self.sender), ("MU_SMTP_RECIPIENT", self.recipient)):
            if not isinstance(address, str) or re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", address) is None:
                raise NotificationError(f"{name} requires one plain mailbox address")
        if not isinstance(self.authorization_code, str) or not self.authorization_code.strip() or any(c in self.authorization_code for c in "\r\n\0"):
            raise NotificationError("MU_SMTP_AUTHORIZATION_CODE is required")

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> SmtpConfig:
        names = ("MU_SMTP_HOST", "MU_SMTP_SENDER", "MU_SMTP_RECIPIENT", "MU_SMTP_AUTHORIZATION_CODE")
        if any(not environment.get(name) for name in names):
            raise NotificationError("missing SMTP configuration; set MU_SMTP_HOST, MU_SMTP_SENDER, MU_SMTP_RECIPIENT and MU_SMTP_AUTHORIZATION_CODE")
        return cls(*(environment[name] for name in names))

    @property
    def target_fingerprint(self) -> str:
        return canonical_sha256({"host": self.host, "sender": self.sender, "recipient": self.recipient})


@dataclass(frozen=True)
class SendResult:
    state: DeliveryState
    code: str
    retryable: bool = False

    def __post_init__(self) -> None:
        codes = {"smtp_accepted", "smtp_recipient_refused", "smtp_authentication_failed", "smtp_rejected",
                 "smtp_tls_error", "smtp_result_unknown", "smtp_connection_failed", "entry_review_expired"}
        if self.state not in {DeliveryState.CONFIRMED, DeliveryState.FAILED, DeliveryState.UNKNOWN} or self.code not in codes:
            raise NotificationError("invalid SMTP result")
        if type(self.retryable) is not bool or (self.retryable and self.state is not DeliveryState.FAILED):
            raise NotificationError("only definite SMTP failure can retry")


class SmtpTransport:
    def __init__(self, config: SmtpConfig, *, factory=smtplib.SMTP_SSL, clock: Clock | None = None):
        self.config = config
        self.factory = factory
        self.target_fingerprint = config.target_fingerprint
        self.clock = clock or SystemClock()

    def send(self, event: AlertEvent) -> SendResult:
        message = render_message(event, self.config)
        client = None
        sending = False
        try:
            client = self.factory(self.config.host, 465, timeout=20, context=ssl.create_default_context())
            client.login(self.config.sender, self.config.authorization_code)
            if event.review_until_ms is not None and not event.occurred_at_ms <= self.clock.now_ms() < event.review_until_ms:
                return SendResult(DeliveryState.FAILED, "entry_review_expired")
            sending = True
            refused = client.send_message(message, from_addr=self.config.sender, to_addrs=[self.config.recipient])
            if refused:
                return SendResult(DeliveryState.FAILED, "smtp_recipient_refused", all(400 <= value[0] < 500 for value in refused.values()))
            return SendResult(DeliveryState.CONFIRMED, "smtp_accepted")
        except smtplib.SMTPAuthenticationError:
            return SendResult(DeliveryState.FAILED, "smtp_authentication_failed")
        except smtplib.SMTPRecipientsRefused as exc:
            return SendResult(DeliveryState.FAILED, "smtp_recipient_refused", bool(exc.recipients) and all(400 <= value[0] < 500 for value in exc.recipients.values()))
        except (smtplib.SMTPSenderRefused, smtplib.SMTPDataError, smtplib.SMTPConnectError, smtplib.SMTPHeloError) as exc:
            return SendResult(DeliveryState.FAILED, "smtp_rejected", 400 <= exc.smtp_code < 500)
        except ssl.SSLError:
            return SendResult(DeliveryState.UNKNOWN if sending else DeliveryState.FAILED, "smtp_tls_error")
        except (smtplib.SMTPException, OSError):
            return SendResult(DeliveryState.UNKNOWN if sending else DeliveryState.FAILED,
                              "smtp_result_unknown" if sending else "smtp_connection_failed", not sending)
        finally:
            if client is not None:
                try:
                    client.close()
                except (OSError, smtplib.SMTPException):
                    pass


def _text(value) -> str:
    return "".join(character if character.isprintable() else " " for character in str(value))[:512]


def _time(value: int | None) -> str:
    if value is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return f"{value} ms since Unix epoch"


def render_message(event: AlertEvent, config: SmtpConfig) -> EmailMessage:
    title = {AlertKind.ENTRY_REVIEW: "入场信号待人工复核", AlertKind.SIGNAL_INVALIDATED: "先前入场提醒已失效",
             AlertKind.SERVICE_FAULT: "信号服务故障记录", AlertKind.SERVICE_RECOVERED: "信号服务恢复记录"}[event.kind]
    reasons = {"ready": "权威扫描结果为 READY_FOR_REVIEW", "decision_changed": "后续扫描已不再满足入场条件",
               "source_unavailable": "数据或运行状态不足，先前提醒已不能继续采信；不代表策略确定反转",
               "review_expired": "提醒复核期限届满，需要等待新的扫描证据",
               "signal_replaced": "后续扫描提供了另一信号或配置", "health_event": "服务已记录的状态变化，非送信时实时状态",
               "runtime_changed": "观察到的服务运行状态变化，扫描健康需另行查询"}
    lines = [title, f"事件 ID: {event.event_id}", f"事件时间 UTC: {_time(event.occurred_at_ms)}", f"依据: {reasons[event.reason]}"]
    observation = event.observation
    if observation is not None:
        lines.extend((f"标的: {_text(observation.symbol)}", f"策略: {_text(observation.strategy_name)}",
                      f"策略配置指纹: {observation.strategy_config_fingerprint}",
                      "策略代码版本: unknown（源观测未记录；配置指纹不是代码版本）",
                      f"generation: {_text(observation.trusted_run_id or 'unknown')}",
                      f"数据哈希: {_text(dict(observation.content_sha256_by_interval))}",
                      f"观察时间 UTC: {_time(observation.observed_at_ms)}",
                      f"扫描决定: {observation.decision_code.value if observation.decision_code else 'unknown'}",
                      f"数据门禁: {observation.trust_reason.value}"))
        if observation.scan_result is not None:
            result = observation.scan_result
            lines.extend((f"信号 K 线开盘时间 UTC: {_time(result.signal_time_ms)}",
                          f"参考触发价: {result.trigger_price}", f"规划初始止损: {result.initial_stop}",
                          f"1h regime: {_text(result.regime_1h)}; RSI: {result.rsi14}; MACD hist: {result.macd_hist}"))
        if event.review_until_ms is not None:
            lines.append(f"本提醒人工复核截止 UTC（不含边界）: {_time(event.review_until_ms)}")
        lines.extend(("失效条件: 后续不再 READY、信号/配置替换、数据或服务不可用、超过复核期限。",
                      "这是人工复核提醒，不是下单或盈利保证；规划止损不是交易所保护单。",
                      "尚无可信实际持仓；加仓、止损调整、退出提醒当前不可用。"))
    if event.related_event_id:
        lines.append(f"关联入场事件 ID: {event.related_event_id}")
    if event.problems:
        lines.append("记录时故障: " + ", ".join(event.problems))
    lines.append("SMTP accepted 只表示服务器接受，不代表进入收件箱或已读；实际成交需人工记录。")
    message = EmailMessage()
    message["From"] = config.sender
    message["To"] = config.recipient
    message["Subject"] = "[MU] " + title
    message["Message-ID"] = f"<mu-{event.event_id}@alerts.invalid>"
    message.set_content("\n".join(lines))
    return message
