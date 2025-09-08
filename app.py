import streamlit as st
import sqlite3
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta, date
import json
import os
from cryptography.fernet import Fernet
import base64
# Nowy import dla Google Gemini
from google import genai
import openai
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import io
from PIL import Image

# --- KONFIGURACJA I INICJALIZACJA ---

SECRET_KEY_SEED = "twoj-bardzo-dlugi-i-tajny-klucz-do-szyfrowania-konfiguracji"
KEY = base64.urlsafe_b64encode(SECRET_KEY_SEED.encode().ljust(32)[:32])
FERNET = Fernet(KEY)

def encrypt_data(data: str) -> bytes:
    return FERNET.encrypt(data.encode())

def decrypt_data(encrypted_data: bytes) -> str:
    return FERNET.decrypt(encrypted_data).decode()

# --- ZARZĄDZANIE BAZĄ DANYCH W PAMIĘCI ---

def get_db_connection():
    if 'db_conn' not in st.session_state:
        st.session_state.db_conn = sqlite3.connect(":memory:", check_same_thread=False)
        init_db(st.session_state.db_conn)
    return st.session_state.db_conn

def init_db(conn):
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sites (
            id INTEGER PRIMARY KEY,
            name TEXT,
            url TEXT UNIQUE,
            username TEXT,
            app_password BLOB,
            image_style_prompt TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS personas (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT)")
    conn.commit()

def db_execute(conn, query, params=(), fetch=None):
    cursor = conn.cursor()
    cursor.execute(query, params)
    if fetch == "one": result = cursor.fetchone()
    elif fetch == "all": result = cursor.fetchall()
    else: result = None
    conn.commit()
    return result

# --- KLASA DO OBSŁUGI WORDPRESS REST API ---
class WordPressAPI:
    def __init__(self, url, username, password):
        self.base_url = url.rstrip('/') + "/wp-json/wp/v2"
        self.auth = HTTPBasicAuth(username, password)

    def _make_request(self, endpoint, params=None, display_error=True):
        try:
            response = requests.get(f"{self.base_url}/{endpoint}", params=params, auth=self.auth, timeout=15)
            response.raise_for_status()
            return response.json(), response.headers
        except requests.exceptions.HTTPError as e:
            if display_error and e.response.status_code != 400:
                 st.error(f"Błąd HTTP ({e.response.status_code}) przy '{endpoint}': {e.response.text}")
        except requests.exceptions.RequestException as e:
            if display_error: st.error(f"Błąd połączenia przy '{endpoint}': {e}")
        return None, {}

    def test_connection(self):
        try:
            response = requests.get(f"{self.base_url}/users/me", auth=self.auth, timeout=10)
            response.raise_for_status()
            return True, "Połączenie udane!"
        except requests.exceptions.HTTPError as e: return False, f"Błąd HTTP ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e: return False, f"Błąd połączenia: {e}"

    def get_stats(self):
        try:
            data, headers = self._make_request("posts", params={"per_page": 1})
            total_posts = int(headers.get('X-WP-Total', 0))
            last_post_date = "Brak" if not data else datetime.fromisoformat(data[0]['date']).strftime('%Y-%m-%d %H:%M')
            return {"total_posts": total_posts, "last_post_date": last_post_date}
        except Exception: return {"total_posts": "Błąd", "last_post_date": "Błąd"}

    def get_all_posts_since(self, start_date):
        all_posts = []
        page = 1
        while True:
            params = {"per_page": 100, "page": page, "after": start_date.isoformat(), "orderby": "date", "order": "asc"}
            posts_data, _ = self._make_request("posts", params=params, display_error=False)
            if not posts_data: break
            all_posts.extend(posts_data)
            page += 1
        return all_posts

    def get_categories(self):
        data, _ = self._make_request("categories", params={"per_page": 100})
        return {cat['name']: cat['id'] for cat in data} if data else {}

    def get_users(self):
        data, _ = self._make_request("users", params={"per_page": 100, "roles": "administrator,editor,author"}, display_error=False)
        return {user['name']: user['id'] for user in data} if data else {}

    def get_posts(self, per_page=50):
        posts_data, _ = self._make_request("posts", params={"per_page": per_page, "orderby": "date", "_embed": True})
        if not posts_data: return []
        is_embedded = '_embedded' in posts_data[0]
        if is_embedded:
            final_posts = []
            for item in posts_data:
                author_name = item['_embedded']['author'][0].get('name', 'N/A')
                author_id = item['_embedded']['author'][0].get('id', 0)
                categories = [t.get('name', '') for tl in item['_embedded'].get('wp:term', []) for t in tl if t.get('taxonomy') == 'category']
                final_posts.append({"id": item['id'], "title": item['title']['rendered'], "date": datetime.fromisoformat(item['date']).strftime('%Y-%m-%d %H:%M'), "author_name": author_name, "author_id": author_id, "categories": ", ".join(filter(None, categories))})
            return final_posts
        else:
            st.warning("Serwer nie zwrócił osadzonych danych. Dociąganie informacji...")
            author_ids = {p['author'] for p in posts_data}
            author_map = {}
            for author_id in author_ids:
                user_data, _ = self._make_request(f"users/{author_id}", display_error=False)
                if user_data:
                    author_map[author_id] = user_data.get('name', 'N/A')
            category_ids = {cid for p in posts_data for cid in p['categories']}
            cat_data, _ = self._make_request("categories", params={"include": ",".join(map(str, category_ids))})
            category_map = {cat['id']: cat['name'] for cat in cat_data or []}
            final_posts = []
            for p in posts_data:
                final_posts.append({"id": p['id'], "title": p['title']['rendered'], "date": datetime.fromisoformat(p['date']).strftime('%Y-%m-%d %H:%M'), "author_name": author_map.get(p['author'], 'N/A'), "author_id": p['author'], "categories": ", ".join(filter(None, [category_map.get(cid, '') for cid in p['categories']]))})
            return final_posts

    def upload_image_from_bytes(self, image_bytes, filename):
        try:
            files = {'file': (filename, image_bytes, 'image/png')}
            upload_response = requests.post(f"{self.base_url}/media", files=files, auth=self.auth, timeout=30)
            upload_response.raise_for_status()
            return upload_response.json().get('id')
        except requests.exceptions.HTTPError as e:
            st.warning(f"Nie udało się wgrać obrazka '{filename}'. Błąd HTTP ({e.response.status_code}): {e.response.text}")
            return None
        except Exception as e:
            st.warning(f"Nie udało się wgrać obrazka z bajtów: {filename}. Błąd ogólny: {e}")
            return None

    def update_post(self, post_id, data):
        try:
            response = requests.post(f"{self.base_url}/posts/{post_id}", json=data, auth=self.auth, timeout=15)
            response.raise_for_status()
            return True, f"Wpis ID {post_id} zaktualizowany."
        except requests.exceptions.HTTPError as e: return False, f"Błąd aktualizacji wpisu ID {post_id} ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e: return False, f"Błąd sieci przy aktualizacji wpisu ID {post_id}: {e}"

    def publish_post(self, title, content, status, publish_date, category_ids, tags, author_id=None, featured_image_bytes=None, meta_title=None, meta_description=None):
        post_data = {'title': title, 'content': content, 'status': status, 'date': publish_date, 'categories': category_ids, 'tags': tags}
        if author_id: post_data['author'] = int(author_id)
        if featured_image_bytes:
            media_id = self.upload_image_from_bytes(featured_image_bytes, f"featured-image-{datetime.now().timestamp()}.png")
            if media_id: post_data['featured_media'] = media_id
        if meta_title or meta_description:
            post_data['meta'] = { "rank_math_title": meta_title, "rank_math_description": meta_description, "_aioseo_title": meta_title, "_aioseo_description": meta_description, "_yoast_wpseo_title": meta_title, "_yoast_wpseo_metadesc": meta_description }
        try:
            response = requests.post(f"{self.base_url}/posts", json=post_data, auth=self.auth, timeout=20)
            response.raise_for_status()
            return True, f"Wpis opublikowany/zaplanowany! ID: {response.json()['id']}", response.json().get('link')
        except requests.exceptions.HTTPError as e: return False, f"Błąd publikacji ({e.response.status_code}): {e.response.text}", None
        except requests.exceptions.RequestException as e: return False, f"Błąd sieci podczas publikacji: {e}", None

# --- LOGIKA GENEROWANIA TREŚCI I PROMPTY ---

HTML_RULES = "Zasady formatowania HTML:\n- NIE UŻYWAJ <h1>.\n- UŻYWAJ WYŁĄCZNIE: <h2>, <h3>, <p>, <b>, <strong>, <ul>, <ol>, <li>, <table>, <tr>, <th>, <td>."
SYSTEM_PROMPT_BASE = f"Jesteś ekspertem SEO i copywriterem. Twoim zadaniem jest tworzenie wysokiej jakości, unikalnych artykułów na bloga. Pisz w języku polskim.\n{HTML_RULES}"

DEFAULT_MASTER_PROMPT_TEMPLATE = """# ROLA I CEL
{{PERSONA_DESCRIPTION}} Twoim celem jest napisanie wyczerpującego, wiarygodnego i praktycznego artykułu na temat "{{TEMAT_ARTYKULU}}", który demonstruje głęboką wiedzę (E-E-A-T).

# ZŁOŻONOŚĆ I DŁUGOŚĆ ARTYKUŁU
Na podstawie wstępnej analizy, temat "{{TEMAT_ARTYKULU}}" został sklasyfikowany jako temat {{ANALIZA_TEMATU}}. Dostosuj długość i głębię artykułu do tej klasyfikacji.

# GRUPA DOCELOWA
Artykuł jest skierowany do {{GRUPA_DOCELOWA}}. Dostosuj język i styl do tej grupy.

# STRUKTURA I GŁĘBIA
**Zasada Odwróconej Piramidy (Answer-First):** Rozpocznij artykuł, wplatając w pierwszy akapit bezpośrednią i zwięzłą odpowiedź na główne pytanie z tematu.
Artykuł musi mieć logiczną strukturę. Rozwiń poniższe kluczowe zagadnienia:
{{ZAGADNIENIA_KLUCZOWE}}

**SEKCJA 'REASONING' DLA AI (BARDZO WAŻNE):**
Szczególną uwagę zwróć na sekcję wyjaśniającą "dlaczego" lub "jak coś działa". Musi być ona samowystarczalna, klarowna i przedstawiona w formie konkretnych kroków lub argumentów. To kluczowy fragment dla systemów AI (Passage Ranking).

# GŁĘBIA SEMANTYCZNA I RELACJE LEKSYKALNE
Aby zademonstrować pełne zrozumienie tematu, wpleć w treść podane poniżej terminy. Użyj **hiperonimów**, aby wprowadzić szerszy kontekst, oraz **hiponimów**, aby podać konkretne przykłady.
- Hiperonimy do wykorzystania: {{HIPERONIMY}}
- Hiponimy do wykorzystania: {{HIPONIMY}}
- Dodatkowe synonimy: {{SYNOMINY}}

# SŁOWA KLUCZOWE
Naturalnie wpleć w treść następujące słowa kluczowe: {{SLOWA_KLUCZOWE}}.
Dodatkowo, wpleć w treść poniższe frazy semantyczne: {{DODATKOWE_SLOWA_SEMANTYCZNE}}.

# STYL, TON I E-E-A-T
- **Doświadczenie (Experience):** Wplataj zwroty wskazujące na osobiste doświadczenie ("Z mojego doświadczenia...", "Częstym błędem jest...").
- **Ekspertyza (Expertise):** Używaj precyzyjnej terminologii, wyjaśniając ją w prosty sposób.
- **Autorytatywność (Authoritativeness):** Pisz w sposób pewny i zdecydowany.
- **Zaufanie (Trustworthiness):** Bądź transparentny, wspominaj o potencjalnych wadach opisywanych rozwiązań.

# FORMATOWANIE
Stosuj się ściśle do zasad formatowania HTML podanych w głównym prompcie systemowym. Używaj pogrubień (<b>, <strong>) dla kluczowych terminów. Rozważ użycie tabeli (<table>) dla danych porównawczych."""

DEFAULT_BRIEF_PROMPT_TEMPLATE = """Jesteś światowej klasy strategiem treści SEO. Twoim zadaniem jest stworzenie szczegółowego briefu dla artykułu na podstawie podanego tematu.

# KROK 1: ANALIZA TEMATU
Przeanalizuj podany temat: "{{TOPIC}}" pod kątem jego złożoności i intencji wyszukiwania. Określ, czy temat jest:
- **SZEROKI**: Wymaga wyczerpującego, długiego artykułu (np. 'pillar page').
- **WĄSKI**: Odpowiada na jedno, konkretne pytanie i wymaga krótszego artykułu.

# KROK 2: TWORZENIE BRIEFU W FORMACIE JSON
Na podstawie analizy z Kroku 1, stwórz brief w formacie JSON.
**KRYTYCZNA ZASADA: Wartość klucza `temat_artykulu` MUSI być DOKŁADNIE taka sama jak temat podany przez użytkownika.**

Struktura JSON:
{
  "temat_artykulu": "{{TOPIC}}",
  "analiza_tematu": "Krótki opis, czy temat jest szeroki czy wąski i dlaczego.",
  "grupa_docelowa": "Krótki opis, dla kogo jest artykuł.",
  "zagadnienia_kluczowe": [
      // Dla tematów SZEROKICH: 5-7 nagłówków (H2).
      // Dla tematów WĄSKICH: 2-4 nagłówki (H2).
      // WAŻNE: Jedno z zagadnień MUSI odpowiadać na pytanie "Dlaczego..." lub "Jak to działa krok po kroku...", aby stworzyć sekcję 'reasoning'.
  ],
  "slowa_kluczowe": [
      // Array 5-10 głównych słów kluczowych.
  ],
  "dodatkowe_slowa_semantyczne": [
      // Array 5-10 fraz i kolokacji semantycznie wspierających główny temat.
  ],
  "relacje_leksykalne": {
      "synonimy": [
          // Array 3-5 synonimów dla głównego słowa kluczowego.
      ],
      "hiperonimy": [
          // Array 2-3 terminów ogólniejszych, nadrzędnych (np. dla "rower" -> "pojazd", "sprzęt sportowy").
      ],
      "hiponimy": [
          // Array 2-3 terminów bardziej szczegółowych, podrzędnych (np. dla "rower" -> "rower górski", "rower szosowy").
      ]
  }
}

Wygeneruj wyłącznie kompletny i poprawny brief w formacie JSON dla tematu: "{{TOPIC}}"
"""

def call_gpt5_nano(api_key, prompt):
    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model="gpt-5-nano",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content

def generate_article_two_parts(api_key, title, prompt):
    part1_prompt = f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\n{prompt}\n\nNapisz PIERWSZĄ POŁOWĘ tego artykułu. Zatrzymaj się w naturalnym miejscu w połowie tekstu."
    part1_text = call_gpt5_nano(api_key, part1_prompt)

    part2_prompt = f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\nOto pierwsza połowa artykułu. Dokończ go, pisząc drugą połowę. Kontynuuj płynnie od miejsca, w którym przerwano. Nie dodawaj wstępów typu 'Oto kontynuacja' ani nie powtarzaj tytułu.\n\nOryginalne wytyczne do artykułu:\n{prompt}\n\n---DOTYCHCZAS NAPISANA TREŚĆ---\n{part1_text}"
    part2_text = call_gpt5_nano(api_key, part2_prompt)

    return title, part1_text.strip() + "\n\n" + part2_text.strip()

def generate_article_dispatcher(model, api_key, title, prompt):
    try:
        if model == "gpt-5-nano":
            return generate_article_two_parts(api_key, title, prompt)
        else:
            return title, f"**BŁĄD: Nieobsługiwany model '{model}'**"
    except Exception as e:
        return title, f"**BŁĄD KRYTYCZNY podczas generowania artykułu:** {str(e)}"

def generate_image_prompt_gpt5(api_key, article_title, style_prompt):
    prompt = f"""Jesteś art directorem. Twoim zadaniem jest stworzenie krótkiego promptu do generatora obrazów AI, łącząc temat artykułu z podanym stylem przewodnim.

# STYL PRZEWODNI (NAJWAŻNIEJSZY)
{style_prompt if style_prompt else "Brak specyficznego stylu, skup się na fotorealizmie."}

# TEMAT ARTYKUŁU DO WIZUALIZACJI
"{article_title}"

# KRYTYCZNE ZASADY - BEZWZGLĘDNIE PRZESTRZEGAJ:
1. Prompt MUSI być w języku angielskim.
2. NIGDY nie używaj słów związanych z tekstem: NIE WOLNO użyć słów takich jak: text, words, letters, typography, caption, title, etc.
3. Zamiast abstrakcyjnych konceptów używaj konkretnych, wizualnych obiektów/scen.
4. Finalny prompt musi zaczynać się od "photorealistic, ...", a kończyć na "no text, no letters, no writing".
5. Zintegruj styl przewodni z wizualizacją tematu w spójny, artystyczny sposób.

Wygeneruj TYLKO gotowy prompt (1-2 zdania)."""
    return call_gpt5_nano(api_key, prompt).strip()

def generate_image_gemini(api_key, image_prompt, aspect_ratio="4:3"):
    try:
        if aspect_ratio not in image_prompt: image_prompt = f"{aspect_ratio} aspect ratio, {image_prompt}"
        if "no text" not in image_prompt.lower(): image_prompt += ", no text, no letters, no writing, no typography"

        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(model="gemini-2.5-flash-image-preview", contents=[image_prompt])

        if response.candidates:
            for part in response.candidates[0].content.parts:
                if part.inline_data is not None: return part.inline_data.data, None

        return None, f"API nie zwróciło obrazka. Sprawdź prompt: {image_prompt}"
    except Exception as e:
        return None, f"Krytyczny błąd podczas komunikacji z API Gemini: {e}"

def generate_brief_and_image(openai_api_key, google_api_key, topic, aspect_ratio, style_prompt, brief_template):
    try:
        final_brief_prompt = brief_template.replace("{{TOPIC}}", topic)
        json_string = call_gpt5_nano(openai_api_key, final_brief_prompt).strip().replace("```json", "").replace("```", "")
        brief_data = json.loads(json_string)
    except Exception as e:
        return topic, {"error": f"Błąd krytyczny podczas generowania briefu: {str(e)}"}, None, None

    try:
        image_prompt = generate_image_prompt_gpt5(openai_api_key, brief_data['temat_artykulu'], style_prompt)
        st.info(f"Generowanie obrazka dla: {brief_data['temat_artykulu']}...")
        st.caption(f"Prompt obrazka: {image_prompt}")
        image_bytes, image_error = generate_image_gemini(google_api_key, image_prompt, aspect_ratio)
        return topic, brief_data, image_bytes, image_error
    except Exception as e:
        return topic, brief_data, None, f"Błąd podczas generowania promptu/obrazka: {e}"

def generate_meta_tags_gpt5(api_key, article_title, article_content, keywords):
    try:
        prompt = f"""Jesteś ekspertem SEO copywritingu. Przeanalizuj poniższy artykuł i stwórz do niego idealne meta tagi. Temat główny: {article_title}. Słowa kluczowe: {", ".join(keywords)}. Treść artykułu (fragment): {article_content[:2500]}. Zwróć odpowiedź WYŁĄCZNIE w formacie JSON z dwoma kluczami: "meta_title" (max 60 znaków) i "meta_description" (max 155 znaków)."""
        json_string = call_gpt5_nano(api_key, prompt).strip().replace("```json", "").replace("```", "")
        return json.loads(json_string)
    except Exception as e:
        return {"meta_title": article_title, "meta_description": ""}

# --- INTERFEJS UŻYTKOWNIKA (STREAMLIT) ---

st.set_page_config(layout="wide", page_title="PBN Manager")

if 'master_prompt' not in st.session_state: st.session_state.master_prompt = DEFAULT_MASTER_PROMPT_TEMPLATE
if 'brief_prompt' not in st.session_state: st.session_state.brief_prompt = DEFAULT_BRIEF_PROMPT_TEMPLATE
if 'menu_choice' not in st.session_state: st.session_state.menu_choice = "Dashboard"
if 'generated_articles' not in st.session_state: st.session_state.generated_articles = []
if 'generated_briefs' not in st.session_state: st.session_state.generated_briefs = []

st.title("🚀 PBN Manager")
st.caption("Centralne zarządzanie i generowanie treści dla Twojej sieci blogów.")

conn = get_db_connection()

st.sidebar.header("Menu Główne")
menu_options = ["Dashboard", "Zarządzanie Stronami", "Zarządzanie Personami", "🗺️ Strateg Tematyczny", "Generator Briefów", "Generowanie Treści", "Harmonogram Publikacji", "Zarządzanie Treścią", "⚙️ Edytor Promptów"]

# --- POPRAWIONA LOGIKA DO PROGRAMOWEJ NAWIGACJI ---
default_index = 0
if 'go_to_page' in st.session_state:
    try:
        default_index = menu_options.index(st.session_state.go_to_page)
        del st.session_state.go_to_page # Usuwamy flagę, aby nie wpływała na kolejne interakcje
    except ValueError:
        default_index = 0 # Na wypadek, gdyby nazwa strony była błędna

st.sidebar.radio("Wybierz sekcję:", menu_options, key='menu_choice', index=default_index)

st.sidebar.header("Konfiguracja API")
openai_api_key = st.secrets.get("OPENAI_API_KEY", "") or st.sidebar.text_input("Klucz OpenAI API", type="password")
google_api_key = st.secrets.get("GOOGLE_API_KEY", "") or st.sidebar.text_input("Klucz Google AI API", type="password")

with st.sidebar.expander("Zarządzanie Konfiguracją (Plik JSON)"):
    uploaded_file = st.file_uploader("Załaduj plik konfiguracyjny", type="json", key="config_uploader")
    if uploaded_file is not None:
        if uploaded_file.file_id != st.session_state.get('last_uploaded_file_id', ''):
            try:
                config_data = json.load(uploaded_file)
                db_execute(conn, "DELETE FROM sites")
                db_execute(conn, "DELETE FROM personas")

                for site in config_data.get('sites', []):
                    encrypted_password_bytes = base64.b64decode(site['app_password_b64'])
                    style_prompt = site.get('image_style_prompt', '')
                    db_execute(conn,
                        "INSERT INTO sites (name, url, username, app_password, image_style_prompt) VALUES (?, ?, ?, ?, ?)",
                        (site['name'], site['url'], site['username'], encrypted_password_bytes, style_prompt)
                    )

                for persona in config_data.get('personas', []):
                    db_execute(conn, "INSERT INTO personas (name, description) VALUES (?, ?)", (persona['name'], persona['description']))

                st.session_state.last_uploaded_file_id = uploaded_file.file_id
                st.success(f"Pomyślnie załadowano {len(config_data.get('sites',[]))} stron i {len(config_data.get('personas',[]))} person!")
                st.rerun()
            except Exception as e:
                st.error(f"Błąd podczas przetwarzania pliku: {e}")

    sites_for_export = db_execute(conn, "SELECT name, url, username, app_password, image_style_prompt FROM sites", fetch="all")
    personas_for_export = db_execute(conn, "SELECT name, description FROM personas", fetch="all")
    if sites_for_export or personas_for_export:
        export_data = {'sites': [], 'personas': []}
        for name, url, username, encrypted_pass_bytes, style_prompt in sites_for_export:
            encrypted_pass_b64 = base64.b64encode(encrypted_pass_bytes).decode('utf-8')
            export_data['sites'].append({
                'name': name,
                'url': url,
                'username': username,
                'app_password_b64': encrypted_pass_b64,
                'image_style_prompt': style_prompt
            })
        for name, description in personas_for_export:
            export_data['personas'].append({'name': name, 'description': description})
        st.download_button(label="Pobierz konfigurację", data=json.dumps(export_data, indent=2), file_name="pbn_config.json", mime="application/json")

# --- GŁÓWNA LOGIKA WYŚWIETLANIA STRON ---

if st.session_state.menu_choice == "Zarządzanie Stronami":
    st.header("🔗 Zarządzanie Stronami i Konfiguracją")
    st.subheader("Dodaj nową stronę")
    with st.form("add_site_form", clear_on_submit=True):
        name = st.text_input("Przyjazna nazwa strony")
        url = st.text_input("URL strony", placeholder="https://twojastrona.pl")
        username = st.text_input("Login WordPress")
        app_password = st.text_input("Hasło Aplikacji", type="password")
        if st.form_submit_button("Testuj połączenie i Zapisz", type="primary"):
            if all([name, url, username, app_password]):
                with st.spinner("Testowanie połączenia..."):
                    api = WordPressAPI(url, username, app_password)
                    success, message = api.test_connection()
                if success:
                    encrypted_password = encrypt_data(app_password)
                    try:
                        db_execute(conn, "INSERT INTO sites (name, url, username, app_password, image_style_prompt) VALUES (?, ?, ?, ?, ?)", (name, url, username, encrypted_password, ""))
                        st.success(f"Strona '{name}' dodana!")
                        st.rerun()
                    except sqlite3.IntegrityError: st.error(f"Strona o URL '{url}' już istnieje.")
                else: st.error(f"Nie udało się dodać strony. Błąd: {message}")
            else: st.error("Wszystkie pola są wymagane.")

    st.subheader("Lista załadowanych stron")
    sites = db_execute(conn, "SELECT id, name, url, username, image_style_prompt FROM sites", fetch="all")
    if not sites: st.info("Brak załadowanych stron.")
    else:
        for site_id, name, url, username, style_prompt in sites:
            with st.container(border=True):
                c1, c2 = st.columns([3, 1])
                c1.markdown(f"**{name}** (`{url}`)")
                if c2.button("🗑️ Usuń", key=f"delete_{site_id}", use_container_width=True):
                    db_execute(conn, "DELETE FROM sites WHERE id = ?", (site_id,))
                    st.rerun()

                with st.expander("Edytuj styl wizualny obrazków dla tej strony"):
                    new_style = st.text_area("Prompt stylu", value=style_prompt or "photorealistic, sharp focus, soft natural lighting", key=f"style_{site_id}", height=100, help="Opisz styl obrazków, np. 'minimalistyczny, flat design, pastelowe kolory' lub 'dramatyczne oświetlenie, styl kinowy, wysoki kontrast'.")
                    if st.button("Zapisz styl", key=f"save_style_{site_id}"):
                        db_execute(conn, "UPDATE sites SET image_style_prompt = ? WHERE id = ?", (new_style, site_id))
                        st.success(f"Styl dla '{name}' zaktualizowany!")
                        st.rerun()

elif st.session_state.menu_choice == "Dashboard":
    st.header("📊 Dashboard Aktywności")
    sites_list = db_execute(conn, "SELECT id, name, url, username, app_password FROM sites", fetch="all")
    if not sites_list:
        st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami'.")
    else:
        st.subheader("Liczba publikacji w czasie")
        time_range_options = {"Ostatnie 7 dni": 7, "Ostatnie 30 dni": 30, "Ostatnie 3 miesiące": 90}
        selected_range_label = st.radio("Wybierz zakres czasu", options=time_range_options.keys(), horizontal=True, label_visibility="collapsed")
        days_to_fetch = time_range_options[selected_range_label]

        @st.cache_data(ttl=600)
        def get_all_posts_for_dashboard(sites_tuple, days):
            start_date = datetime.now() - timedelta(days=days)
            all_posts_dates = []
            def fetch_site_posts(site_data):
                _, _, url, username, enc_pass = site_data
                api = WordPressAPI(url, username, decrypt_data(enc_pass))
                return [p['date'] for p in api.get_all_posts_since(start_date)]

            with ThreadPoolExecutor() as executor:
                futures = {executor.submit(fetch_site_posts, site): site for site in sites_tuple}
                for future in as_completed(futures):
                    all_posts_dates.extend(future.result())
            return all_posts_dates

        with st.spinner(f"Pobieranie danych o publikacjach z {len(sites_list)} stron..."):
            post_data = get_all_posts_for_dashboard(tuple(sites_list), days_to_fetch)

        if not post_data:
            st.info("Brak opublikowanych wpisów w wybranym okresie.")
        else:
            df_posts = pd.DataFrame(post_data, columns=['date'])
            df_posts['date_only'] = pd.to_datetime(df_posts['date']).dt.date
            posts_by_day = df_posts.groupby('date_only').size().reset_index(name='count')
            posts_by_day = posts_by_day.set_index('date_only')
            date_range = pd.date_range(start=date.today() - timedelta(days=days_to_fetch-1), end=date.today())
            posts_by_day = posts_by_day.reindex(date_range.date, fill_value=0)
            posts_by_day.index.name = "Data"
            posts_by_day.columns = ["Liczba publikacji"]
            st.bar_chart(posts_by_day)

        st.subheader("Ogólne statystyki")
        @st.cache_data(ttl=600)
        def get_summary_stats(sites_tuple):
            all_data = []
            for _, name, url, username, encrypted_pass in sites_tuple:
                api = WordPressAPI(url, username, decrypt_data(encrypted_pass))
                stats = api.get_stats()
                all_data.append({"Nazwa": name, "URL": url, "Liczba wpisów": stats['total_posts'], "Ostatni wpis": stats['last_post_date']})
            return all_data

        if st.button("Odśwież statystyki"): st.cache_data.clear()
        stats_data = get_summary_stats(tuple(sites_list))
        st.dataframe(pd.DataFrame(stats_data), use_container_width=True, hide_index=True)

elif st.session_state.menu_choice == "Zarządzanie Personami":
    st.header("🎭 Zarządzanie Personami")
    with st.expander("Dodaj nową Personę", expanded=True):
        with st.form("add_persona_form", clear_on_submit=True):
            persona_name = st.text_input("Nazwa Persony")
            persona_desc = st.text_area("Opis Persony", height=150, help="Opisz kim jest autor, jakie ma doświadczenie i styl.")
            if st.form_submit_button("Zapisz Personę"):
                if persona_name and persona_desc:
                    try:
                        db_execute(conn, "INSERT INTO personas (name, description) VALUES (?, ?)", (persona_name, persona_desc))
                        st.success(f"Persona '{persona_name}' zapisana!")
                    except sqlite3.IntegrityError: st.error(f"Persona o nazwie '{persona_name}' już istnieje.")
                else: st.error("Nazwa i opis nie mogą być puste.")

    st.subheader("Lista zapisanych Person")
    personas = db_execute(conn, "SELECT id, name, description FROM personas", fetch="all")
    if not personas: st.info("Brak zapisanych Person.")
    else:
        for id, name, desc in personas:
            with st.expander(f"**{name}**"):
                st.text_area("Opis", value=desc, height=100, disabled=True, key=f"desc_{id}")
                if st.button("Usuń", key=f"delete_persona_{id}"):
                    db_execute(conn, "DELETE FROM personas WHERE id = ?", (id,))
                    st.rerun()

elif st.session_state.menu_choice == "🗺️ Strateg Tematyczny":
    st.header("🗺️ Strateg Tematyczny")
    st.info("To narzędzie analizuje wszystkie opublikowane wpisy na wybranej stronie, grupuje je w klastry tematyczne i proponuje nowe tematy, aby wypełnić luki i wzmocnić autorytet w danej dziedzinie.")

    sites_list = db_execute(conn, "SELECT id, name, url, username, app_password FROM sites", fetch="all")
    sites_options = {site[1]: site for site in sites_list}

    if not sites_options:
        st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami'.")
    else:
        site_name = st.selectbox("Wybierz stronę do analizy", options=sites_options.keys())

        if st.button("Analizuj i Zaplanuj Klastry", type="primary"):
            site_info = sites_options[site_name]
            api = WordPressAPI(site_info[2], site_info[3], decrypt_data(site_info[4]))

            with st.spinner(f"Pobieranie tytułów artykułów ze strony '{site_name}'..."):
                all_posts = []
                page = 1
                while True:
                    posts_data, _ = api._make_request("posts", params={"per_page": 100, "page": page, "_fields": "title.rendered"})
                    if not posts_data:
                        break
                    all_posts.extend(posts_data)
                    page += 1
                
                all_titles = [p['title']['rendered'] for p in all_posts]

            if not all_titles:
                st.error("Nie znaleziono żadnych artykułów na tej stronie.")
            else:
                with st.spinner("AI analizuje strukturę tematyczną i szuka luk..."):
                    CLUSTER_ANALYSIS_PROMPT = f"""Jesteś ekspertem SEO i strategiem treści. Twoim zadaniem jest analiza poniższej listy tytułów artykułów z bloga.

Twoje zadania:
1.  **Pogrupuj tytuły w logiczne klastry tematyczne.** Nazwa klastra powinna być ogólnym, nadrzędnym tematem (np. "Marketing w mediach społecznościowych", "Pozycjonowanie stron WWW", "Zdrowe odżywianie").
2.  **Dla każdego klastra, zidentyfikuj luki w treści.** Pomyśl, jakich fundamentalnych lub uzupełniających tematów brakuje, aby klaster był kompletny i wyczerpujący.
3.  **Zaproponuj 3-5 nowych, konkretnych tematów artykułów,** które wypełnią te luki i wzmocnią autorytet w ramach klastra. Nowe tematy powinny być angażujące i odpowiadać na potencjalne pytania użytkowników.

Przeanalizuj tę listę tytułów:
{'- ' + '\n- '.join(all_titles)}

Zwróć wynik WYŁĄCZNIE w formacie JSON. Struktura powinna być listą klastrów, gdzie każdy klaster jest obiektem z kluczami: "nazwa_klastra", "istniejace_artykuly" (lista tytułów), oraz "proponowane_nowe_tematy" (lista nowych tytułów).

Przykład:
[
  {{
    "nazwa_klastra": "SEO dla początkujących",
    "istniejace_artykuly": ["Jak wybrać dobre słowa kluczowe?", "Co to jest link building?"],
    "proponowane_nowe_tematy": ["Kompletny przewodnik po SEO On-Page dla nowicjuszy", "Czym jest audyt SEO i jak go przeprowadzić samemu?", "Najczęstsze błędy w SEO, których musisz unikać"]
  }}
]
"""
                    try:
                        response_str = call_gpt5_nano(openai_api_key, CLUSTER_ANALYSIS_PROMPT).strip().replace("```json", "").replace("```", "")
                        cluster_data = json.loads(response_str)
                        st.session_state.cluster_analysis_result = cluster_data
                    except Exception as e:
                        st.error(f"Błąd podczas analizy przez AI: {e}")
                        st.session_state.cluster_analysis_result = None

    if 'cluster_analysis_result' in st.session_state and st.session_state.cluster_analysis_result:
        st.subheader("Wyniki Analizy i Propozycje Treści")
        
        all_new_topics = []
        for cluster in st.session_state.cluster_analysis_result:
            with st.expander(f"**Klaster: {cluster['nazwa_klastra']}** ({len(cluster['istniejace_artykuly'])} istniejących, {len(cluster['proponowane_nowe_tematy'])} propozycji)"):
                st.markdown("##### Istniejące artykuły w klastrze:")
                for title in cluster['istniejace_artykuly']:
                    st.write(f"- {title}")
                
                st.markdown("##### 💡 Proponowane nowe tematy do wypełnienia luki:")
                for new_topic in cluster['proponowane_nowe_tematy']:
                    st.write(f"- **{new_topic}**")
                    all_new_topics.append(new_topic)

        st.subheader("Akcje")
        if all_new_topics:
            if st.button("Dodaj wszystkie proponowane tematy do Generatora Briefów", type="primary"):
                if 'topics_from_strategist' not in st.session_state:
                    st.session_state.topics_from_strategist = ""
                
                existing_topics = st.session_state.topics_from_strategist.split('\n')
                new_topics_set = set(all_new_topics)
                
                final_topics = existing_topics + [t for t in new_topics_set if t not in existing_topics]
                st.session_state.topics_from_strategist = "\n".join(filter(None, final_topics))
                
                st.session_state.go_to_page = "Generator Briefów"
                st.success(f"{len(new_topics_set)} unikalnych tematów dodanych! Przechodzenie do Generatora Briefów...")
                st.rerun()

elif st.session_state.menu_choice == "Generator Briefów":
    st.header("📝 Generator Briefów")

    initial_topics = ""
    if 'topics_from_strategist' in st.session_state and st.session_state.topics_from_strategist:
        initial_topics = st.session_state.topics_from_strategist
        del st.session_state.topics_from_strategist

    if not (openai_api_key and google_api_key):
        st.error("Wprowadź klucz OpenAI API oraz Google AI API w panelu bocznym.")
    else:
        topics_input = st.text_area("Wprowadź tematy artykułów (jeden na linię)", value=initial_topics, height=250)

        st.subheader("Ustawienia generowania")
        c1, c2 = st.columns(2)
        aspect_ratio = c1.selectbox("Format obrazka", options=["4:3", "16:9", "1:1", "3:2"])

        site_styles = {"Domyślny (Fotorealizm)": ""}
        for name, style in db_execute(conn, "SELECT name, image_style_prompt FROM sites", fetch="all"):
            if style: site_styles[f"Styl: {name}"] = style
        selected_style_label = c2.selectbox("Styl wizualny obrazków", options=site_styles.keys())
        selected_style_prompt = site_styles[selected_style_label]

        if st.button("Generuj briefy i obrazki", type="primary"):
            topics = [topic.strip() for topic in topics_input.split('\n') if topic.strip()]
            if topics:
                st.session_state.generated_briefs = []
                with st.spinner(f"Generowanie {len(topics)} briefów..."):
                    for topic in topics:
                        _, brief, img, err = generate_brief_and_image(openai_api_key, google_api_key, topic, aspect_ratio, selected_style_prompt, st.session_state.brief_prompt)
                        st.session_state.generated_briefs.append({ "topic": topic, "brief": brief, "image": img, "image_error": err })
                st.success("Generowanie zakończone!")
            else: st.error("Wpisz przynajmniej jeden temat.")

        if st.session_state.generated_briefs:
            st.subheader("Wygenerowane Briefy")
            if st.button("Przejdź do generowania artykułów"):
                st.session_state.go_to_page = "Generowanie Treści"
                st.rerun()
            for i, item in enumerate(st.session_state.generated_briefs):
                with st.expander(f"**{i+1}. {item['brief'].get('temat_artykulu', item['topic'])}**"):
                    c1, c2 = st.columns(2)
                    c1.json(item['brief'])
                    with c2:
                        if item['image']: st.image(item['image'], use_column_width=True)
                        if item['image_error']: st.warning(item['image_error'])

elif st.session_state.menu_choice == "Generowanie Treści":
    st.header("🤖 Generator Treści AI")
    if not st.session_state.generated_briefs: st.warning("Brak briefów. Przejdź do 'Generator Briefów'.")
    else:
        personas = {name: desc for _, name, desc in db_execute(conn, "SELECT id, name, description FROM personas", fetch="all")}
        if not personas: st.error("Brak Person. Przejdź do 'Zarządzanie Personami'.")
        else:
            c1, c2 = st.columns(2)
            persona_name = c1.selectbox("Wybierz Personę autora", options=personas.keys())
            c2.info("Model: **gpt-5-nano**")

            valid_briefs = [b for b in st.session_state.generated_briefs if 'error' not in b['brief']]
            if valid_briefs:
                df = pd.DataFrame(valid_briefs)
                df['Zaznacz'] = False
                df['Temat'] = df['brief'].apply(lambda x: x.get('temat_artykulu', 'B/D'))
                df['Ma obrazek'] = df['image'].apply(lambda x: "✅" if x else "❌")

                with st.form("article_generation_form"):
                    edited_df = st.data_editor(df[['Zaznacz', 'Temat', 'Ma obrazek']], hide_index=True, use_container_width=True)
                    if st.form_submit_button("Generuj zaznaczone artykuły", type="primary"):
                        indices = edited_df[edited_df.Zaznacz].index.tolist()
                        if indices:
                            tasks = []
                            for i in indices:
                                brief = valid_briefs[i]['brief']
                                relacje = brief.get("relacje_leksykalne", {})
                                
                                prompt = st.session_state.master_prompt \
                                    .replace("{{PERSONA_DESCRIPTION}}", personas[persona_name]) \
                                    .replace("{{TEMAT_ARTYKULU}}", brief.get("temat_artykulu", "")) \
                                    .replace("{{ANALIZA_TEMATU}}", "SZEROKI" if "szeroki" in brief.get("analiza_tematu", "").lower() else "WĄSKI") \
                                    .replace("{{GRUPA_DOCELOWA}}", brief.get("grupa_docelowa", "")) \
                                    .replace("{{ZAGADNIENIA_KLUCZOWE}}", "\n".join(f"- {z}" for z in brief.get("zagadnienia_kluczowe", []))) \
                                    .replace("{{SLOWA_KLUCZOWE}}", ", ".join(brief.get("slowa_kluczowe", []))) \
                                    .replace("{{DODATKOWE_SLOWA_SEMANTYCZNE}}", ", ".join(brief.get("dodatkowe_slowa_semantyczne", []))) \
                                    .replace("{{HIPERONIMY}}", ", ".join(relacje.get("hiperonimy", []))) \
                                    .replace("{{HIPONIMY}}", ", ".join(relacje.get("hiponimy", []))) \
                                    .replace("{{SYNOMINY}}", ", ".join(relacje.get("synonimy", [])))
                                
                                tasks.append({'title': brief['temat_artykulu'], 'prompt': prompt, 'keywords': brief.get('slowa_kluczowe', []), 'image': valid_briefs[i]['image']})

                            st.session_state.generated_articles = []
                            with st.spinner(f"Generowanie {len(tasks)} artykułów..."):
                                with ThreadPoolExecutor(max_workers=5) as executor:
                                    futures = {executor.submit(generate_article_dispatcher, "gpt-5-nano", openai_api_key, t['title'], t['prompt']): t for t in tasks}
                                    for future in as_completed(futures):
                                        task = futures[future]
                                        title, content = future.result()
                                        meta = generate_meta_tags_gpt5(openai_api_key, title, content, task['keywords'])
                                        st.session_state.generated_articles.append({"title": title, "content": content, "image": task['image'], **meta})
                            st.success("Generowanie zakończone!")
                            st.session_state.go_to_page = "Harmonogram Publikacji"
                            st.rerun()

elif st.session_state.menu_choice == "Harmonogram Publikacji":
    st.header("🗓️ Harmonogram Publikacji")
    if not st.session_state.generated_articles: st.warning("Brak wygenerowanych artykułów.")
    else:
        sites_list = db_execute(conn, "SELECT id, name, url, username, app_password FROM sites", fetch="all")
        sites_options = {site[1]: site for site in sites_list}
        if not sites_options: st.warning("Brak załadowanych stron.")
        else:
            df = pd.DataFrame(st.session_state.generated_articles)
            df['Zaznacz'] = True
            df['Ma obrazek'] = df['image'].apply(lambda x: "✅" if x else "❌")

            with st.form("bulk_schedule_form"):
                st.subheader("1. Wybierz artykuły do publikacji")
                edited_df = st.data_editor(df[['Zaznacz', 'title', 'Ma obrazek', 'meta_title', 'meta_description']], hide_index=True, use_container_width=True, column_config={"title": "Tytuł", "Ma obrazek": st.column_config.TextColumn("Obrazek", width="small"), "meta_title": "Meta Tytuł", "meta_description": "Meta Opis"})

                st.subheader("2. Ustawienia publikacji")
                c1, c2 = st.columns(2)
                selected_sites = c1.multiselect("Wybierz strony docelowe", options=sites_options.keys())
                author_id = c2.number_input("ID Autora (opcjonalnie)", min_value=1, step=1)

                cat_site = st.selectbox("Pobierz kategorie ze strony:", options=sites_options.keys())
                api = WordPressAPI(sites_options[cat_site][2], sites_options[cat_site][3], decrypt_data(sites_options[cat_site][4]))
                categories = api.get_categories()
                selected_cats = st.multiselect("Wybierz kategorie", options=categories.keys())
                tags_str = st.text_input("Tagi (oddzielone przecinkami)")

                st.subheader("3. Planowanie")
                c1,c2,c3 = st.columns(3)
                start_date_val = c1.date_input("Data pierwszego wpisu", datetime.now())
                start_time_val = c2.time_input("Godzina pierwszego wpisu", datetime.now().time())
                interval = c3.number_input("Odstęp (godziny)", min_value=1, value=8)

                if st.form_submit_button("Zaplanuj zaznaczone artykuły", type="primary"):
                    selected = edited_df[edited_df.Zaznacz]
                    if not selected.empty and selected_sites:
                        pub_time = datetime.combine(start_date_val, start_time_val)
                        tags_list = [tag.strip() for tag in tags_str.split(',') if tag.strip()]

                        with st.spinner("Planowanie publikacji..."):
                            for index, row in selected.iterrows():
                                article = st.session_state.generated_articles[index]
                                for site_name in selected_sites:
                                    site_info = sites_options[site_name]
                                    api_pub = WordPressAPI(site_info[2], site_info[3], decrypt_data(site_info[4]))
                                    
                                    site_cats = api_pub.get_categories()
                                    cat_ids = [site_cats[name] for name in selected_cats if name in site_cats]

                                    success, msg, _ = api_pub.publish_post(
                                        title=row['title'],
                                        content=article['content'],
                                        status="future",
                                        publish_date=pub_time.isoformat(),
                                        category_ids=cat_ids,
                                        tags=tags_list,
                                        author_id=(author_id if author_id > 0 else None),
                                        featured_image_bytes=article.get('image'),
                                        meta_title=row['meta_title'],
                                        meta_description=row['meta_description']
                                    )
                                    if success: st.success(f"[{site_name}]: {msg}")
                                    else: st.error(f"[{site_name}]: {msg}")
                                
                                pub_time += timedelta(hours=interval)
                        st.balloons()

elif st.session_state.menu_choice == "Zarządzanie Treścią":
    st.header("✏️ Zarządzanie Treścią")
    sites_list = db_execute(conn, "SELECT id, name, url, username, app_password FROM sites", fetch="all")
    sites_options = {site[1]: site for site in sites_list}
    if sites_options:
        site_name = st.selectbox("Wybierz stronę", options=sites_options.keys())
        site_info = sites_options[site_name]
        api = WordPressAPI(site_info[2], site_info[3], decrypt_data(site_info[4]))

        @st.cache_data(ttl=300)
        def get_site_content(_site_name):
            posts, categories, users = api.get_posts(per_page=100), api.get_categories(), api.get_users()
            return posts, categories, users

        posts, categories, users = get_site_content(site_name)
        if posts:
            df = pd.DataFrame(posts)
            df['Zaznacz'] = False
            edited_df = st.data_editor(df[['Zaznacz', 'id', 'title', 'date', 'author_name', 'categories']].rename(columns={'author_name': 'autor'}), disabled=['id', 'title', 'date', 'autor', 'categories'], hide_index=True)
            selected_posts = edited_df[edited_df.Zaznacz]
            if not selected_posts.empty:
                with st.form("bulk_edit_form"):
                    st.subheader(f"Masowa edycja dla {len(selected_posts)} wpisów")
                    new_cats = st.multiselect("Zastąp kategorie", options=categories.keys())
                    new_author = st.selectbox("Zmień autora", options=[None] + list(users.keys()))
                    if st.form_submit_button("Wykonaj"):
                        data = {}
                        if new_cats: data['categories'] = [categories[c] for c in new_cats]
                        if new_author: data['author'] = users[new_author]
                        if data:
                            with st.spinner("Aktualizowanie..."):
                                for post_id in selected_posts['id']:
                                    success, msg = api.update_post(post_id, data)
                                    if success: st.success(msg)
                                    else: st.error(msg)
                            st.cache_data.clear()
                            st.rerun()

elif st.session_state.menu_choice == "⚙️ Edytor Promptów":
    st.header("⚙️ Edytor Promptów")
    st.info("Dostosuj szablony promptów używane do generowania briefów i artykułów. Zmiany są aktywne w bieżącej sesji.")
    tab1, tab2 = st.tabs(["Master Prompt (Artykuły)", "Prompt do Briefu"])
    with tab1:
        st.subheader("Master Prompt do generowania artykułów")
        st.markdown("**Zmienne:** `{{PERSONA_DESCRIPTION}}`, `{{TEMAT_ARTYKULU}}`, `{{ANALIZA_TEMATU}}`, `{{GRUPA_DOCELOWA}}`, `{{ZAGADNIENIA_KLUCZOWE}}`, `{{SLOWA_KLUCZOWE}}`, `{{DODATKOWE_SLOWA_SEMANTYCZNE}}`, `{{HIPERONIMY}}`, `{{HIPONIMY}}`, `{{SYNOMINY}}`")
        st.session_state.master_prompt = st.text_area("Edytuj Master Prompt", value=st.session_state.master_prompt, height=600, label_visibility="collapsed")
        if st.button("Przywróć domyślny Master Prompt"):
            st.session_state.master_prompt = DEFAULT_MASTER_PROMPT_TEMPLATE
            st.rerun()
    with tab2:
        st.subheader("Prompt do generowania briefu")
        st.markdown("**Zmienne:** `{{TOPIC}}`")
        st.session_state.brief_prompt = st.text_area("Edytuj Prompt do Briefu", value=st.session_state.brief_prompt, height=600, label_visibility="collapsed")
        if st.button("Przywróć domyślny Prompt do Briefu"):
            st.session_state.brief_prompt = DEFAULT_BRIEF_PROMPT_TEMPLATE
            st.rerun()
