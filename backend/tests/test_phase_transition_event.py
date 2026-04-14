from llm.types import ChunkType, LLMChunk


def test_chunk_type_has_phase_transition_and_agent_status():
    assert ChunkType.PHASE_TRANSITION.value == "phase_transition"
    assert ChunkType.AGENT_STATUS.value == "agent_status"


def test_llm_chunk_accepts_phase_info_and_agent_status():
    chunk = LLMChunk(
        type=ChunkType.PHASE_TRANSITION,
        phase_info={
            "from_phase": 1,
            "to_phase": 3,
            "from_step": None,
            "to_step": "brief",
        },
    )
    assert chunk.phase_info["to_phase"] == 3

    chunk2 = LLMChunk(
        type=ChunkType.AGENT_STATUS,
        agent_status={"stage": "thinking", "iteration": 0},
    )
    assert chunk2.agent_status["stage"] == "thinking"
