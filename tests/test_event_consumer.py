from lark_meeting_voice.lark.event_consumer import build_meeting_event_jq
from lark_meeting_voice.memory.meeting_memory import MeetingMemory


def test_build_meeting_event_jq_filters_current_meeting_only():
    meeting_id = "7649380316335246569"

    assert build_meeting_event_jq(
        "vc.meeting.participant_meeting_ended_v1", meeting_id
    ) == 'select(.meeting_id == "7649380316335246569")'
    assert build_meeting_event_jq(
        "vc.note.generated_v1", meeting_id
    ) == (
        'select(.note_source.source_type == "meeting" and '
        '.note_source.source_entity_id == "7649380316335246569")'
    )
    assert build_meeting_event_jq(
        "minutes.minute.generated_v1", meeting_id
    ) == (
        'select(.minute_source.source_type == "meeting" and '
        '.minute_source.source_entity_id == "7649380316335246569")'
    )


def test_meeting_memory_includes_current_meeting_event_artifacts():
    memory = MeetingMemory()
    assert memory.add_meeting_event(
        "vc.note.generated_v1",
        {
            "event_id": "evt-note-1",
            "note_token": "note-token",
            "verbatim_token": "verbatim-token",
            "note_source": {
                "source_type": "meeting",
                "source_entity_id": "meeting-1",
            },
        },
    )
    assert memory.apply_artifact_content(
        "note",
        "note-token",
        "Decision: ship the event pipeline this week.\nOwner: James",
    )
    assert memory.add_meeting_event(
        "minutes.minute.generated_v1",
        {
            "event_id": "evt-minute-1",
            "minute_token": "minute-token",
            "title": "Weekly Sync",
            "minute_source": {
                "source_type": "meeting",
                "source_entity_id": "meeting-1",
            },
        },
    )
    assert memory.add_meeting_event(
        "vc.meeting.participant_meeting_ended_v1",
        {
            "event_id": "evt-end-1",
            "meeting_id": "meeting-1",
            "meeting_no": "616633662",
            "topic": "Weekly Sync",
            "end_time": "2026-06-10T11:35:00+08:00",
        },
    )

    block = memory.build_context_block("summarize this meeting")

    assert "Current meeting event metadata:" in block
    assert "- Meeting ID: meeting-1" in block
    assert "- Topic: Weekly Sync" in block
    assert "Current meeting generated artifacts:" in block
    assert "- note token=note-token status=ready" in block
    assert "- verbatim token=verbatim-token status=pending" in block
    assert "- minute token=minute-token title=Weekly Sync status=pending" in block
    assert "Current meeting artifact excerpts:" in block
    assert "Decision: ship the event pipeline this week." in block
