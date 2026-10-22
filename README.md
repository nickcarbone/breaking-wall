BREAKING WALL — UPGRADE (upload in this order through github.com)

1. scripts/  → open the scripts folder in the repo, Add file → Upload files,
   drop in 1-scripts/validate_streams.py, commit.
2. .github/workflows/ → open that folder in the repo, Add file → Upload files,
   drop in both files from 2-github-workflows/, commit.
3. Repo root → Add file → Upload files, drop in index.html and
   news_sources.json from 3-repo-root/, commit. This push starts a
   validation run straight away (Actions tab).
4. Delete manage.html (open it → ⋯ menu → Delete file).

Nothing else to set up: the YOUTUBE_API_KEY secret you already have is reused,
and the dead-stream issue label creates itself on first run.
