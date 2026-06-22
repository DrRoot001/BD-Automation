from module3.utils.storage import safe_filename


def test_safe_filename_basic():
    assert safe_filename("John Doe", default="candidate", extension=".pdf") == "John_Doe.pdf"
    assert safe_filename("John Doe", default="candidate", extension="") == "John_Doe"


def test_safe_filename_handles_special_chars():
    assert safe_filename("Mary-Jane O'Connor", default="candidate", extension=".pdf") == "Mary-Jane_O_Connor.pdf"
    assert safe_filename(" /\\:*?\"<>| ", default="candidate", extension=".pdf") == "candidate.pdf"


def test_safe_filename_collapses_underscores_and_trims():
    assert safe_filename("  Alice   Smith  ", default="candidate", extension=".pdf") == "Alice_Smith.pdf"
    assert safe_filename("__A__B__", default="candidate", extension=".pdf") == "A_B.pdf"
