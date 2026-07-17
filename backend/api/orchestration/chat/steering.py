from __future__ import annotations

import logging

from agent.steering import drain_steer_queue

from api.orchestration.chat.events import event_json

logger = logging.getLogger(__name__)


def close_run_steering(session: dict, agent: object) -> list[str]:
    """Stop accepting steering and explicitly reject anything left queued."""
    queue = session.pop("_steer_queue", None)
    agent.steer_queue = None
    return [
        event_json(
            {
                "type": "agent_status",
                "stage": "steering_ack",
                "message": "本轮已结束，未能应用这条引导，请重新发送",
                "text": text,
            }
        )
        for text in drain_steer_queue(queue)
    ]


def clear_run_steering(session: dict, agent: object) -> None:
    """Teardown-time cleanup. 取消/断连路径无法再向客户端 yield 终结 ack，
    残留引导只能在这里记录，不静默吞掉。正常路径 close_run_steering 已清空队列。
    """
    queue = session.pop("_steer_queue", None)
    agent.steer_queue = None
    for text in drain_steer_queue(queue):
        logger.warning(
            "Steering dropped at run teardown without terminal ack: %s", text
        )
