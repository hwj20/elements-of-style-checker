"""Build samples/sample.pdf: a small test document full of known rule violations.

It deliberately includes things that make PDF extraction hard: a running
header, page numbers, a paragraph that crosses a page break, and words divided
at line ends (some divided badly, one correctly).

Run:  python samples/make_sample_pdf.py
"""
from pathlib import Path

import pymupdf

OUT = Path(__file__).with_name("sample.pdf")

TITLE = "A Short Report on Our Field Trip"

# Each item: ("h", text) heading | ("p", text) paragraph | ("lines", [..]) paragraph with fixed line breaks
CONTENT = [
    ("h", TITLE),
    ("p", "One of the most interesting experiences of the year was our trip to the coast. "
          "The trip was planned by the committee in a hasty manner, and the question as to whether we "
          "should go at all was raised more than once. Owing to the fact that the weather was bad, we "
          "left late. Walking slowly down the road, the bus appeared at last. We packed food, maps and "
          "blankets for the night."),
    ("p", "The guide, Mr. Harris was waiting at the station. He was a man who had traveled all over the "
          "world. It was nearly noon, we could not reach the cliffs before dark. However, we decided to "
          "try. The data shows that most everybody enjoyed the walk."),
    ("p", "I met an old fisherman on the pier several years ago. A man who had lived on the coast all his "
          "life. He told us that the sea was different than it used to be, and that there were less boats "
          "in the harbour every year. He was not very often wrong about such things."),
    ("h", "Notes on the Evening Play"),
    ("p", "In the play, Hamlet learns that his father was murdered by his uncle. He decides to feign "
          "madness in order to test the king. Later he killed Polonius by mistake and is sent to England. "
          "Ophelia, overwhelmed by grief, drowns herself."),
    ("p", "Formerly, science was taught by the textbook method, while now the laboratory method is "
          "employed. It was both a long ceremony and very tedious. He only found two mistakes in the "
          "articles about his adventures in Spain, which were published in the local newspaper."),
    ("p", "Being in a dilapidated condition, I was able to buy the house very cheap. My brother you will "
          "be pleased to hear, is now in perfect health. The situation is perilous but there is still one "
          "chance of escape. Mr. B. also spoke along the same lines, and his speech was literally a flood "
          "of abuse."),
    ("p", "The commitee will seperate the reports immediatly, and it's members will read them untill "
          "Friday. Alright, the results were definately better than last year."),
    ("lines", [
        "Strunk thought that a writer should know the principles of word division as well as",
        "he knows his grammar. Careful printers divide the word knowl-",
        "edge only by mistake, and a good compositor never sets des-",
        "cribe or leaves a single letter such as the first one of a-",
        "bout at the end of a line. A well-printed pic-",
        "ture of a page shows how each division follows the formation of the word.",
    ]),
    ("p", "I went to his house yesterday, (my third attempt to see him) but he had left town. Keats "
          "declares that \"beauty is truth, truth beauty.\" Aristotle says \"Art is an imitation of "
          "nature.\" We met on August ninth to discuss Chapter Twelve of the report."),
    ("p", "The third concert of the series was given last evening, and a large audience was in "
          "attendance. Mr. Appleton was the soloist, and the orchestra furnished the music. The former "
          "showed himself an artist of the first rank, while the latter proved worthy of its reputation. "
          "The interest aroused by the series has been gratifying, and it is planned to give a similar "
          "series next year. The fourth concert will be given in May, when an equally attractive "
          "programme will be presented."),
    ("p", "The food at the hotel was very good."),
    ("p", "Dead leaves covered the ground. Somewhere in the house a guitar hummed sleepily. The army was "
          "rapidly mobilized, and by spring it had reached the border. In the fifth book of The "
          "Excursion, Wordsworth gives a minute description of this church."),
]

PAGE_W, PAGE_H = 612, 792
LEFT, RIGHT, TOP, BOTTOM = 72, 540, 80, 720
FONT, BOLD = "tiro", "tibo"
SIZE, LEAD = 11, 15


class Writer:
    def __init__(self):
        self.doc = pymupdf.open()
        self.page = None
        self.y = 0
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=PAGE_W, height=PAGE_H)
        n = self.doc.page_count
        self.page.insert_text((LEFT, 45), "Field Trip Report - Draft for Review", fontname=FONT, fontsize=9)
        self.page.insert_text((PAGE_W / 2 - 4, 760), str(n), fontname=FONT, fontsize=9)
        self.y = TOP

    def line(self, text, font=FONT, size=SIZE, lead=LEAD):
        if self.y + lead > BOTTOM:
            self.new_page()
        self.y += lead
        self.page.insert_text((LEFT, self.y), text, fontname=font, fontsize=size)

    def paragraph(self, text):
        words, cur = text.split(), ""
        for w in words:
            trial = (cur + " " + w).strip()
            if pymupdf.get_text_length(trial, fontname=FONT, fontsize=SIZE) > RIGHT - LEFT:
                self.line(cur)
                cur = w
            else:
                cur = trial
        if cur:
            self.line(cur)
        self.y += 8  # paragraph spacing

    def fixed_lines(self, lines):
        for ln in lines:
            self.line(ln)
        self.y += 8

    def heading(self, text):
        self.y += 6
        self.line(text, font=BOLD, size=15, lead=22)
        self.y += 6


def main():
    w = Writer()
    # push the 'concert' paragraph across a page break by filling page 1 first
    for kind, payload in CONTENT:
        if kind == "h":
            w.heading(payload)
        elif kind == "p":
            w.paragraph(payload)
        else:
            w.fixed_lines(payload)
    w.doc.save(OUT)
    print(f"wrote {OUT} ({w.doc.page_count} pages)")


if __name__ == "__main__":
    main()
