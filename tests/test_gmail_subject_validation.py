import unittest

from gmail_integration import is_valid_recipient_email, subject_has_line_break


class SubjectValidationTests(unittest.TestCase):
    def test_normal_subject_with_r_and_n_is_allowed(self):
        self.assertFalse(subject_has_line_break("Reminder for new client"))

    def test_carriage_return_is_rejected(self):
        self.assertTrue(subject_has_line_break("Hello\rInjected: value"))

    def test_line_feed_is_rejected(self):
        self.assertTrue(subject_has_line_break("Hello\nInjected: value"))

    def test_subject_without_line_break_is_allowed(self):
        self.assertFalse(subject_has_line_break("Quick update — proposal"))


class RecipientValidationTests(unittest.TestCase):
    def test_valid_recipient_is_allowed(self):
        self.assertTrue(is_valid_recipient_email("client@example.com"))

    def test_crlf_header_injection_is_rejected(self):
        self.assertFalse(is_valid_recipient_email("client@example.com\r\nBcc: other@example.com"))

    def test_multiple_recipients_in_one_field_are_rejected(self):
        self.assertFalse(is_valid_recipient_email("client@example.com,other@example.com"))


if __name__ == "__main__":
    unittest.main()
