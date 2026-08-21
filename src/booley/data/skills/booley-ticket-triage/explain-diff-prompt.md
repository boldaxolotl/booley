# Explain Diff

Prepare a grounded, structured explanation of the specified code change for a
human reviewer. Explore the surrounding code and the supplied immutable evidence.

Return plain-text data for these sections:

- Background sections: begin with beginner context, then narrow to the existing
  system directly affected by the change.
- Intuition sections: explain the essential idea with concrete toy examples and
  clear data or control flow.
- Code references: identify the repository, repository-relative path, revision,
  and a high-level walkthrough for each important change.
- Findings: call out important consequences, edge cases, and uncertainties.
- Quiz: exactly five medium-difficulty multiple-choice questions. Each question
  needs at least two choices, exactly one correct answer, and useful feedback for
  every choice.

Write engaging, direct prose. Do not return HTML, CSS, JavaScript, Markdown
markup, external links, or control characters. Booley validates these records and
owns every presentation surface, including the trusted interactive quiz renderer.
