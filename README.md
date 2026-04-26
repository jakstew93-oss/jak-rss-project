# Jak RSS Project

Build your own RSS feed for finding news. This project reads public RSS feeds, filters stories by keywords, removes duplicates, and writes a personalized RSS feed to `public/feed.xml`.

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python src/build_feed.py
```

Open `public/feed.xml` in an RSS reader, or serve the folder locally:

```bash
python3 -m http.server 8000 --directory public
```

Your local feed URL will be:

```text
http://localhost:8000/feed.xml
```

## Customize It

Edit `config/feeds.yml`.

- Add RSS feeds under `sources`.
- Add topics under `filters.include_keywords`.
- Add things you never want under `filters.exclude_keywords`.
- Empty `include_keywords` if you want every story from every source.

Most Guardian section pages expose RSS by adding `/rss` to the section URL. BBC News publishes feeds such as `https://feeds.bbci.co.uk/news/rss.xml` and `https://feeds.bbci.co.uk/news/technology/rss.xml`.

## Publish It

The generated feed is just a static XML file. This repo includes a GitHub Actions workflow that publishes `public/feed.xml` to GitHub Pages every 30 minutes.

Once GitHub Pages is enabled, your feed URL will look like:

```text
https://jakstew93-oss.github.io/jak-rss-project/feed.xml
```

You can paste that URL into an RSS reader on your iPhone.

To rebuild it manually on your Mac:

```bash
python src/build_feed.py
```

You can also trigger a rebuild from GitHub by opening the `Publish RSS feed` workflow and clicking `Run workflow`.
