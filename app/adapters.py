"""模拟适配器:电子签名与通知。

真实环境会对接 CA 签名服务和短信/邮件网关,这里用本地实现替代:
- 签名:生成可校验的伪签名串,并追加到签名日志文件;
- 通知:写入 notifications 表(页面可见)并追加到通知日志文件。
接口保持与真实适配器一致,便于日后替换。
"""
import hashlib
import os
from datetime import datetime

from .db import dirs_for


class MockSignatureAdapter:
    """模拟电子签名:返回确定性伪签名串,留痕到本地日志。"""

    def __init__(self, db_path):
        self.log_file = os.path.join(dirs_for(db_path)["logs"], "signatures.log")

    def sign(self, agreement_id, party, signer_name, signed_at):
        raw = f"{agreement_id}|{party}|{signer_name}|{signed_at}|mock-ca"
        token = "SIG-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16].upper()
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{signed_at} agreement={agreement_id} party={party} "
                    f"signer={signer_name} token={token}\n")
        return token


class MockNotificationAdapter:
    """模拟通知:落库(页面通知中心可见)+ 本地日志文件。"""

    def __init__(self, db_path):
        self.log_file = os.path.join(dirs_for(db_path)["logs"], "notifications.log")

    def notify(self, conn, user_key, message, now=None):
        now = now or datetime.now().isoformat(timespec="seconds")
        conn.execute(
            "INSERT INTO notifications(user_key, message, created_at) VALUES(?,?,?)",
            (user_key, message, now),
        )
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(f"{now} -> {user_key}: {message}\n")
