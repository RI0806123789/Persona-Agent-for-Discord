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
            content_moderator, memory_anniversary, memory_projects, memory_service, moderation_prompts,
            reminder_schedule, reminder_service, stats_graphs, stats_normalization, stats_service,
        )
        self.assertIs(memory_service.merge_anniversary_profile, memory_anniversary.merge_anniversary_profile)
        self.assertIs(memory_service.default_projects_memory, memory_projects.default_projects_memory)
        self.assertIs(reminder_service.next_occurrence, reminder_schedule.next_occurrence)
        self.assertIs(reminder_service._local_to_utc, reminder_schedule.local_to_utc)
        self.assertIs(stats_service.normalize_topics, stats_normalization.normalize_topics)
        self.assertIs(stats_service.BIG_FIVE_LABELS, stats_normalization.BIG_FIVE_LABELS)
        for name in (
            "VERDICT_SAFE", "VERDICT_UNSAFE", "VERDICT_UNKNOWN", "_DATA_START", "_DATA_END",
            "_INPUT_UNSAFE_DECISION_KEY", "_INPUT_UNSAFE_DECISION_QUESTION", "NG_WORD_MATCH_KEY",
            "_SYSTEM_PROMPT", "_VOICE_TURN_SYSTEM_PROMPT", "_build_prompt", "_build_combined_system_prompt",
            "_normalize_emotion_delta", "_normalize_reminder", "_normalize_safe_flag", "_response_schema",
        ):
            self.assertIs(getattr(content_moderator, name), getattr(moderation_prompts, name), name)
        for name in ("render_hourly_graph", "render_topic_graph", "render_bigfive_radar", "render_ranking_graph"):
            self.assertTrue(callable(getattr(stats_graphs, name)), name)

    def test_memory_service_no_longer_takes_bot_state(self):
        import inspect

        from persona_bot.services.memory_service import MemoryService
        # BotState は受け取らない。usage は内部の Gemini 呼び出しのトークン記録用の任意引数（#480）。
        parameters = inspect.signature(MemoryService.__init__).parameters
        self.assertEqual(list(parameters), ["self", "genai_client", "db", "usage"])
        self.assertIsNone(parameters["usage"].default)


class VoiceAndViewSplitTests(unittest.TestCase):
    """#470 の分割後も、従来の import 先・ロガー名・クラス構成が変わらないことを確かめる。"""

    def test_voice_conversation_parts_keep_the_original_names(self):
        from persona_bot.services import voice_conversation_playback, voice_conversation_service
        original = voice_conversation.logger.name
        self.assertEqual(voice_conversation_playback.logger.name, original)
        self.assertEqual(voice_conversation_service.logger.name, original)
        self.assertTrue(issubclass(voice_conversation.VoiceConversation, voice_conversation_playback.VoicePlaybackMixin))
        # 従来の `from voice_conversation import VoiceConversationService` は遅延で再公開される（循環を避けるため）。
        self.assertIs(voice_conversation.VoiceConversationService, voice_conversation_service.VoiceConversationService)
        for name in ("_start_playback", "_abort_playback", "_fade_out_playback", "_discard_response", "_bot_audible"):
            self.assertTrue(hasattr(voice_conversation.VoiceConversation, name), name)
        self.assertFalse(hasattr(voice_conversation.VoiceConversation, "_playback"))

    def test_playback_state_is_held_in_one_object(self):
        from persona_bot.services.voice_conversation_state import _PlaybackState
        state = _PlaybackState()
        self.assertIsNone(state.source)
        self.assertIsNone(state.owner)
        self.assertFalse(state.started)
        self.assertIsNone(state.fading)
        self.assertEqual(state.last_audible_at, float("-inf"))

    def test_tts_parts_are_reachable_from_voice(self):
        from persona_bot.audio import speech_text, tts_synthesis, voice
        self.assertIs(voice.build_speech_text, speech_text.build_speech_text)
        self.assertIs(voice._synthesize_pcm, tts_synthesis._synthesize_pcm)
        self.assertEqual(tts_synthesis.logger.name, voice.logger.name)

    def test_view_parts_reexports_the_split_helpers(self):
        from persona_bot.views import panel_authorization, panel_state, view_parts
        self.assertIs(view_parts.edit_panel_message, panel_state.edit_panel_message)
        self.assertIs(view_parts.begin_panel_operation, panel_state.begin_panel_operation)
        self.assertIs(view_parts.authorize_administrator_operation, panel_authorization.authorize_administrator_operation)
        self.assertEqual(panel_state.logger.name, view_parts.logger.name)
        self.assertEqual(panel_authorization.logger.name, view_parts.logger.name)
        self.assertTrue(callable(view_parts.PanelView.run_exclusive))

    def test_agent_thread_state_is_split_but_reachable_from_the_original_module(self):
        """#480: インメモリの状態を agent_thread_state へ分けても、従来の import 先・ロガー名を保つ。"""
        from persona_bot.views import agent_thread, agent_thread_state
        for name in ("AgentWebhooks", "AgentWorkThreads", "WorkThreadEntry"):
            self.assertIs(getattr(agent_thread, name), getattr(agent_thread_state, name), name)
        self.assertEqual(agent_thread_state.logger.name, agent_thread.logger.name)
        self.assertEqual(agent_thread.logger.name, "persona_bot.views.agent_thread")
        # 状態側は進捗の表示（Discord への投稿）を持たない。
        for name in ("AgentThreadProgress", "start_agent_progress", "archive_work_thread"):
            self.assertFalse(hasattr(agent_thread_state, name), name)

    def test_agent_thread_state_does_not_import_the_progress_module(self):
        import ast
        from pathlib import Path

        from persona_bot.views import agent_thread_state
        tree = ast.parse(Path(agent_thread_state.__file__).read_text(encoding="utf-8-sig"))
        imported = {
            node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
        } | {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
        self.assertNotIn("persona_bot.views.agent_thread", imported)  # 循環参照になる

    def test_mixins_declare_host_contracts_under_type_checking_and_voice_split_keeps_reexports(self):
        """#495: 分割された全Mixinの TYPE_CHECKING 宣言と、voice_coordinator / diagnostics 分離の契約を検証する。"""
        import ast
        from pathlib import Path

        from persona_bot.core import voice_commands, voice_coordinator
        from persona_bot.services import (
            auto_topic_anniversary, voice_conversation_diagnostics, voice_conversation_playback,
        )
        self.assertIs(voice_commands.VoiceCommandCoordinator, voice_coordinator.VoiceCommandCoordinator)
        self.assertIs(voice_commands.skip_superseded_voice_operation, voice_coordinator.skip_superseded_voice_operation)
        self.assertEqual(voice_conversation_diagnostics.logger.name, voice_conversation.logger.name)
        self.assertTrue(
            issubclass(voice_conversation.VoiceConversation, voice_conversation_diagnostics.VoiceDiagnosticsMixin)
        )
        for name in ("_log_barge_in_stats", "_log_echo_stats", "_log_latency", "_run_sender", "_clear_send_queue"):
            self.assertTrue(hasattr(voice_conversation.VoiceConversation, name), name)

        # VoiceModerationMixin の TYPE_CHECKING import は遅延再公開ではなく実体モジュールを参照する。
        mod_tree = ast.parse(Path(voice_conversation_moderation.__file__).read_text(encoding="utf-8-sig"))
        type_checking_imports = {
            (node.module, alias.name)
            for node in ast.walk(mod_tree)
            if isinstance(node, ast.ImportFrom) and node.module
            for alias in node.names
        }
        self.assertIn(("persona_bot.services.voice_conversation_service", "VoiceConversationService"), type_checking_imports)
        self.assertNotIn(("persona_bot.services.voice_conversation", "VoiceConversationService"), type_checking_imports)

        for module, class_name in (
            (voice_conversation_consent, "VoiceConsentMixin"),
            (voice_conversation_moderation, "VoiceModerationMixin"),
            (voice_conversation_playback, "VoicePlaybackMixin"),
            (voice_conversation_diagnostics, "VoiceDiagnosticsMixin"),
            (autonomous_speech, "AutonomousSpeechMixin"),
            (autonomous_lifecycle, "AutonomousLifecycleMixin"),
            (auto_topic_anniversary, "AnniversaryMixin"),
        ):
            with self.subTest(mixin=class_name):
                tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8-sig"))
                cls_node = next(
                    node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
                )
                has_type_checking = any(
                    isinstance(stmt, ast.If)
                    and isinstance(stmt.test, ast.Name)
                    and stmt.test.id == "TYPE_CHECKING"
                    and stmt.body
                    for stmt in cls_node.body
                )
                self.assertTrue(has_type_checking)


if __name__ == "__main__":
    unittest.main()
