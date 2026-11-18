"""責務ごとの分割（#415）が、公開クラス・ロガー名・状態の境界を変えていないことを確かめる。

運用ログの文言とロガー名は管理コンソール（`privacy.ts` など）が参照するため、切り出したモジュールも
分割前と同じロガー名で出力する。
"""

from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from persona_bot.services import (
    autonomous_lifecycle, autonomous_service, autonomous_speech, voice_conversation, voice_conversation_consent,
    voice_conversation_moderation, voice_conversation_state,
)


class LoggerNameTests(unittest.TestCase):
    def test_voice_conversation_parts_keep_the_original_logger(self):
        original = voice_conversation.logger.name
        self.assertEqual(original, "persona_bot.services.voice_conversation")
        self.assertEqual(voice_conversation_moderation.logger.name, original)
        self.assertEqual(voice_conversation_consent.logger.name, original)

    def test_autonomous_speech_keeps_the_original_logger(self):
        self.assertEqual(autonomous_service.logger.name, "persona_bot.services.autonomous_service")
        self.assertEqual(autonomous_speech.logger.name, autonomous_service.logger.name)


class ClassCompositionTests(unittest.TestCase):
    def test_public_classes_are_composed_from_the_split_modules(self):
        conversation = voice_conversation.VoiceConversation
        self.assertTrue(issubclass(conversation, voice_conversation_consent.VoiceConsentMixin))
        self.assertTrue(issubclass(conversation, voice_conversation_moderation.VoiceModerationMixin))
        self.assertTrue(issubclass(autonomous_service.AutonomousService, autonomous_speech.AutonomousSpeechMixin))
        # 従来の import 先（テストや他のモジュールが使う名前）を保つ
        for name in ("ConsentState", "_Participant", "_SpeakerState", "_TranscriptState", "_Utterance"):
            self.assertIs(getattr(voice_conversation, name), getattr(voice_conversation_state, name), name)
        self.assertTrue(callable(autonomous_service.describe_quiet_hours))
        self.assertTrue(callable(autonomous_service.compute_talk_desire))

    def test_moved_behaviour_is_still_reachable_on_the_public_classes(self):
        for name in ("grant_voice", "withdraw", "_judge_input", "_judge_output", "_remember", "_notify", "_edit_panel"):
            self.assertTrue(hasattr(voice_conversation.VoiceConversation, name), name)
        for name in ("handle_comment", "_speak_opening", "_speak_monologue", "can_speak_session", "_screen_speech"):
            self.assertTrue(hasattr(autonomous_service.AutonomousService, name), name)

    def test_state_is_reached_through_the_state_objects_not_bridge_properties(self):
        for name in ("_participants", "_reservations", "_active_user", "_speaking", "_transcript_owner",
                     "_transcript_blocked", "_transcript_blocked_since", "_transcript_next", "_interrupted"):
            self.assertFalse(hasattr(voice_conversation.VoiceConversation, name), name)


class ServiceSplitTests(unittest.TestCase):
    """#469 の分割後も、従来の import 先・ロガー名・クラス構成が変わらないことを確かめる。"""

    def test_split_modules_keep_the_original_logger(self):
        from persona_bot.services import auto_topic_anniversary, auto_topic_service, reminder_schedule, reminder_service
        self.assertEqual(autonomous_lifecycle.logger.name, autonomous_service.logger.name)
        self.assertEqual(auto_topic_anniversary.logger.name, auto_topic_service.logger.name)
        self.assertEqual(reminder_schedule.logger.name, reminder_service.logger.name)

    def test_public_classes_are_composed_from_the_split_modules(self):
        from persona_bot.services import auto_topic_anniversary, auto_topic_service
        self.assertTrue(issubclass(autonomous_service.AutonomousService, autonomous_lifecycle.AutonomousLifecycleMixin))
        self.assertTrue(issubclass(auto_topic_service.AutoTopicService, auto_topic_anniversary.AnniversaryMixin))
        for name in ("_start_session", "_connect_session", "stop_session", "stop_all_sessions", "_clear_session"):
            self.assertTrue(hasattr(autonomous_service.AutonomousService, name), name)
        for name in ("run_anniversary_checks", "_celebrate_user", "_post_anniversary"):
            self.assertTrue(hasattr(auto_topic_service.AutoTopicService, name), name)
        self.assertIs(autonomous_service._StartAttempt, autonomous_lifecycle._StartAttempt)

    def test_pure_helpers_are_still_reachable_from_the_original_modules(self):
        from persona_bot.services import (
            memory_anniversary, memory_projects, memory_service, reminder_schedule, reminder_service,
            stats_graphs, stats_normalization, stats_service,
        )
        self.assertIs(memory_service.merge_anniversary_profile, memory_anniversary.merge_anniversary_profile)
        self.assertIs(memory_service.default_projects_memory, memory_projects.default_projects_memory)
        self.assertIs(reminder_service.next_occurrence, reminder_schedule.next_occurrence)
        self.assertIs(reminder_service._local_to_utc, reminder_schedule.local_to_utc)
        self.assertIs(stats_service.normalize_topics, stats_normalization.normalize_topics)
        self.assertIs(stats_service.BIG_FIVE_LABELS, stats_normalization.BIG_FIVE_LABELS)
        for name in ("render_hourly_graph", "render_topic_graph", "render_bigfive_radar", "render_ranking_graph"):
            self.assertTrue(callable(getattr(stats_graphs, name)), name)

    def test_memory_service_no_longer_takes_bot_state(self):
        import inspect

        from persona_bot.services.memory_service import MemoryService
        self.assertEqual(list(inspect.signature(MemoryService.__init__).parameters), ["self", "genai_client", "db"])


if __name__ == "__main__":
    unittest.main()
