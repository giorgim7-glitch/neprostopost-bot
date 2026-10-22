"""Build the standalone HTML for a separate static host. No Node build required."""
import argparse
import html as html_utils
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent


def build(origin, destination):
    parsed = urlsplit(origin)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.path not in ('','/') or parsed.query or parsed.fragment:
        raise ValueError('Укажите HTTPS-адрес сервера без /api, пути или параметров')
    origin = f'{parsed.scheme}://{parsed.netloc}'
    html = (ROOT/'static/index.html').read_text()
    js = (ROOT/'static/app.js').read_text()
    html = html.replace('<head>', '<head>\n    <!-- Адрес сервера Ботхост: без /api и без слеша в конце. -->\n    <meta name="neprostopost-api" content="'+html_utils.escape(origin, quote=True)+'">',1)
    html = html.replace('<script src="/static/app.js" defer></script>', '<script>\n'+js.replace('</script','<\\/script')+'\n</script>')
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(html)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--api-origin', default='https://bot-1792478387-3401-gogamisabi.bothost.tech')
    args = parser.parse_args()
    build(args.api_origin, ROOT/'frontend/index.html')
    print('Created frontend/index.html. Verify the backend address in the neprostopost-api meta tag.')
