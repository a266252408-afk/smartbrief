"""在事件产生时记录时间；复制旧记录时保留原时间与未知状态。"""
from datetime import datetime


class EventLogs(list):
    def append(self, event):
        super().append(self._stamp(event))

    def extend(self, events):
        for event in events:
            self.append(event)

    def insert(self, index, event):
        super().insert(index, self._stamp(event))

    @staticmethod
    def _stamp(event):
        return {"created_at": datetime.now().astimezone().isoformat(timespec="milliseconds"), **event}
