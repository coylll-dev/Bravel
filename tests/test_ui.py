import io
import os
import re
import unittest
from unittest.mock import patch

from rich.cells import cell_len

from bravel.ui import UI


class FormattingTests(unittest.TestCase):
    def test_answers_render_markdown_lists_and_tables_without_literal_markers(self):
        stream = io.StringIO()
        UI("never", stream).answer("**Сеть**\n\n- `Ethernet`: локальная сеть.\n\n| Процесс | ЦП |\n| --- | ---: |\n| chrome | 2.5% |")
        output = stream.getvalue()
        self.assertIn("ОТВЕТ", output)
        self.assertIn("Ethernet", output)
        self.assertIn("2.5%", output)
        self.assertNotIn("**", output)
        self.assertNotIn("`", output)
        self.assertNotIn("---:", output)
        self.assertNotIn("\033", output)

    def test_raw_command_preview_preserves_literal_markdown_and_markup(self):
        command = 'echo "**hello** `literal` [red]value[/red]"'
        stream = io.StringIO()
        UI("never", stream).panel("КОМАНДА", command)
        self.assertIn(command, stream.getvalue())

    def test_narrow_unicode_panels_have_equal_display_width_and_closed_borders(self):
        for markdown in (True, False):
            stream = io.StringIO()
            with patch("bravel.ui.shutil.get_terminal_size", return_value=os.terminal_size((36, 24))):
                UI("never", stream).panel("РЕЗУЛЬТАТ", "**Пример**\n\n- 界面 é 😀 " * 5, markdown=markdown)
            lines = stream.getvalue().splitlines()
            self.assertEqual(len({cell_len(line) for line in lines}), 1)
            self.assertTrue(lines[0].endswith("╮"))
            self.assertTrue(lines[-1].endswith("╯"))
            self.assertTrue(all(line.endswith("│") for line in lines[1:-1]))

    def test_model_control_sequences_cannot_rewrite_terminal(self):
        stream = io.StringIO()
        UI("never", stream).answer("**текст**\033[2J\rскрытый\u202e")
        self.assertNotIn("\033", stream.getvalue())
        self.assertNotIn("\r", stream.getvalue())
        self.assertNotIn("\u202e", stream.getvalue())

    def test_color_changes_style_without_changing_visible_text(self):
        outputs = []
        for color in ("always", "never"):
            stream = io.StringIO()
            with patch("bravel.ui.shutil.get_terminal_size", return_value=os.terminal_size((60, 24))):
                UI(color, stream).answer("**INCY** и `chrome`")
            outputs.append(re.sub(r"\033\[[0-9;]*m", "", stream.getvalue()))
        self.assertEqual(outputs[0], outputs[1])


if __name__ == "__main__":
    unittest.main()
