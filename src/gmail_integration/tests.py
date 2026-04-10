from django.test import TestCase
from gmail_integration.service import is_likely_invoice

class GmailHeuristicTests(TestCase):
    def test_pdf_invoice_detected(self):
        # PDF + filename keyword = 2 + 3 = 5 (>= 3)
        self.assertTrue(is_likely_invoice("invoice_123.pdf", "application/pdf", "Hello", "test@example.com"))

    def test_pdf_subject_detected(self):
        # PDF + subject keyword = 2 + 2 = 4 (>= 3)
        self.assertTrue(is_likely_invoice("doc.pdf", "application/pdf", "Your Invoice", "test@example.com"))

    def test_pdf_sender_detected(self):
        # PDF + sender keyword = 2 + 1 = 3 (>= 3)
        self.assertTrue(is_likely_invoice("doc.pdf", "application/pdf", "Hello", "billing@company.com"))

    def test_generic_pdf_rejected(self):
        # PDF only = 2 (< 3)
        self.assertFalse(is_likely_invoice("document.pdf", "application/pdf", "Hello", "friend@example.com"))

    def test_image_receipt_detected(self):
        # Image + filename keyword = 1 + 3 = 4 (>= 3)
        self.assertTrue(is_likely_invoice("receipt.jpg", "image/jpeg", "Hello", "test@example.com"))

    def test_image_generic_rejected(self):
        # Image + sender = 1 + 1 = 2 (< 3)
        self.assertFalse(is_likely_invoice("photo.jpg", "image/jpeg", "Hello", "billing@example.com"))

    def test_inv_regex_prevents_invitation(self):
        # PDF + "invitation" in filename. "invitation" won't match our regex \binv[o\d\W]
        # Score: 2 (PDF) + 0 (fn) + 0 (sub) + 0 (snd) = 2
        self.assertFalse(is_likely_invoice("party_invitation.pdf", "application/pdf", "You are invited", "friend@example.com"))

    def test_inv_prefix_matches(self):
        # PDF + "inv-123" in filename matches \binv[o\d\W]
        # Score: 2 (PDF) + 3 (fn) = 5
        self.assertTrue(is_likely_invoice("inv-123.pdf", "application/pdf", "Hello", "test@example.com"))

    def test_unsupported_mime_rejected(self):
        self.assertFalse(is_likely_invoice("malicious.exe", "application/x-msdownload", "Invoice", "billing@example.com"))
