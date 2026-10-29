import unittest

from gmail_integration import subject_has_line_break


class SubjectValidationTests(unittest.TestCase):
    def test_normal_subject_with_r_and_n_is_allowed(self):
        self.assertFalse(subject_has_line_break("Reminder for new client"))

    def test_carriage_return_is_rejected(self):
        self.assertTrue(subject_has_line_break("Hello\rInjected: value"))

    def test_line_feed_is_rejected(self):
        self.assertTrue(subject_has_line_break("Hello\nInjected: value"))

    def test_subject_without_line_break_is_allowed(self):
        self.assertFalse(subject_has_line_break("Quick update — proposal"))


if __name__ == "__main__":
    unittest.main()
