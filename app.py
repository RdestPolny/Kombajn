import streamlit as st
import sqlite3
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime, timedelta
import json
import os
from cryptography.fernet import Fernet
import base64
import google.generativeai as genai
import openai
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import io
import re

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
    cursor.execute("CREATE TABLE IF NOT EXISTS sites (id INTEGER PRIMARY KEY, name TEXT, url TEXT UNIQUE, username TEXT, app_password BLOB)")
    cursor.execute("CREATE TABLE IF NOT EXISTS personas (id INTEGER PRIMARY KEY, name TEXT UNIQUE, description TEXT)")
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS interlinks (
        id INTEGER PRIMARY KEY, keyword TEXT NOT NULL UNIQUE, keyword_variations TEXT,
        url TEXT NOT NULL, source_post_id INTEGER, source_site_id INTEGER, type TEXT NOT NULL DEFAULT 'manual'
    )""")
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
            return response.json()
        except requests.exceptions.HTTPError as e:
            if display_error: st.error(f"Błąd HTTP ({e.response.status_code}) przy '{endpoint}': {e.response.text}")
        except requests.exceptions.RequestException as e:
            if display_error: st.error(f"Błąd połączenia przy '{endpoint}': {e}")
        return None

    def test_connection(self):
        try:
            response = requests.get(f"{self.base_url}/users/me", auth=self.auth, timeout=10)
            response.raise_for_status()
            return True, "Połączenie udane!"
        except requests.exceptions.HTTPError as e: return False, f"Błąd HTTP ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e: return False, f"Błąd połączenia: {e}"

    def get_stats(self):
        try:
            response = requests.get(f"{self.base_url}/posts", params={"per_page": 1}, auth=self.auth, timeout=10)
            response.raise_for_status()
            total_posts = int(response.headers.get('X-WP-Total', 0))
            last_post_date = "Brak" if not response.json() else datetime.fromisoformat(response.json()[0]['date']).strftime('%Y-%m-%d %H:%M')
            return {"total_posts": total_posts, "last_post_date": last_post_date}
        except Exception: return {"total_posts": "Błąd", "last_post_date": "Błąd"}

    def get_categories(self):
        data = self._make_request("categories", params={"per_page": 100})
        return {cat['name']: cat['id'] for cat in data} if data else {}

    def get_users(self):
        data = self._make_request("users", params={"per_page": 100, "roles": "administrator,editor,author"}, display_error=False)
        return {user['name']: user['id'] for user in data} if data else {}

    def get_posts(self, per_page=50):
        posts_data = self._make_request("posts", params={"per_page": per_page, "orderby": "date", "_embed": True})
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
                user_data = self._make_request(f"users/{author_id}", display_error=False)
                if user_data:
                    author_map[author_id] = user_data.get('name', 'N/A')
            category_ids = {cid for p in posts_data for cid in p['categories']}
            category_map = {cat['id']: cat['name'] for cat in self._make_request("categories", params={"include": ",".join(map(str, category_ids))}) or []}
            final_posts = []
            for p in posts_data:
                final_posts.append({"id": p['id'], "title": p['title']['rendered'], "date": datetime.fromisoformat(p['date']).strftime('%Y-%m-%d %H:%M'), "author_name": author_map.get(p['author'], 'N/A'), "author_id": p['author'], "categories": ", ".join(filter(None, [category_map.get(cid, '') for cid in p['categories']]))})
            return final_posts

    def upload_image(self, image_url):
        try:
            response = requests.get(image_url, timeout=20)
            response.raise_for_status()
            image_bytes = io.BytesIO(response.content)
            filename = os.path.basename(urlparse(image_url).path)
            if not filename: filename = "uploaded_image.jpg"
            headers = {'Content-Disposition': f'attachment; filename={filename}'}
            upload_response = requests.post(f"{self.base_url}/media", headers=headers, files={'file': image_bytes}, auth=self.auth)
            upload_response.raise_for_status()
            return upload_response.json().get('id')
        except Exception as e:
            st.warning(f"Nie udało się wgrać obrazka z URL: {image_url}. Błąd: {e}")
            return None

    def update_post(self, post_id, data):
        try:
            response = requests.post(f"{self.base_url}/posts/{post_id}", json=data, auth=self.auth, timeout=15)
            response.raise_for_status()
            return True, f"Wpis ID {post_id} zaktualizowany."
        except requests.exceptions.HTTPError as e: return False, f"Błąd aktualizacji wpisu ID {post_id} ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e: return False, f"Błąd sieci przy aktualizacji wpisu ID {post_id}: {e}"

    def publish_post(self, title, content, status, publish_date, category_ids, tags, featured_image_url=None, meta_title=None, meta_description=None):
        post_data = {'title': title, 'content': content, 'status': status, 'date': publish_date, 'categories': category_ids, 'tags': tags}
        if featured_image_url:
            media_id = self.upload_image(featured_image_url)
            if media_id:
                post_data['featured_media'] = media_id
        if meta_title or meta_description:
            post_data['meta'] = {
                "rank_math_title": meta_title, "rank_math_description": meta_description,
                "_aioseo_title": meta_title, "_aioseo_description": meta_description,
                "_yoast_wpseo_title": meta_title, "_yoast_wpseo_metadesc": meta_description
            }
        try:
            response = requests.post(f"{self.base_url}/posts", json=post_data, auth=self.auth, timeout=20)
            response.raise_for_status()
            return True, f"Wpis opublikowany/zaplanowany! ID: {response.json()['id']}", response.json().get('link')
        except requests.exceptions.HTTPError as e: return False, f"Błąd publikacji ({e.response.status_code}): {e.response.text}", None
        except requests.exceptions.RequestException as e: return False, f"Błąd sieci podczas publikacji: {e}", None

# --- LOGIKA GENEROWANIA TREŚCI I LINKOWANIA ---
HTML_RULES = "Zasady formatowania HTML:\n- NIE UŻYWAJ <h1>.\n- UŻYWAJ WYŁĄCZNIE: <h2>, <h3>, <p>, <b>, <strong>, <ul>, <ol>, <li>, <table>, <tr>, <th>, <td>."
SYSTEM_PROMPT_BASE = f"Jesteś ekspertem SEO i copywriterem. Twoim zadaniem jest tworzenie wysokiej jakości, unikalnych artykułów na bloga. Pisz w języku polskim.\n{HTML_RULES}"
MASTER_PROMPT_TEMPLATE = """# ROLA I CEL
{{PERSONA_DESCRIPTION}} Twoim celem jest napisanie wyczerpującego, wiarygodnego i praktycznego artykułu na temat "{{TEMAT_ARTYKULU}}", który demonstruje głęboką wiedzę (Ekspertyza), autentyczne doświadczenie (Doświadczenie), jest autorytatywny w tonie (Autorytatywność) i buduje zaufanie czytelnika (Zaufanie).

# GRUPA DOCELOWA
Artykuł jest skierowany do {{GRUPA_DOCELOWA}}. Używaj języka, który jest dla nich zrozumiały, ale nie unikaj terminologii branżowej – wyjaśniaj ją w prosty sposób.

# STRUKTURA I GŁĘBIA
**Zasada Odwróconej Piramidy:** Rozpocznij artykuł od razu od konkretnej i zwięzłej odpowiedzi na główne pytanie zawarte w tytule. Pierwszy akapit (lead) musi dostarczać natychmiastowej wartości. Dopiero w dalszej części rozwiń temat szczegółowo.
Artykuł musi mieć logiczną strukturę. Rozwiń temat w kilku kluczowych sekcjach, a zakończ praktycznym podsumowaniem.
Kluczowe zagadnienia do poruszenia:
{{ZAGADNIENIA_KLUCZOWE}}

# STYL I TON
- **Doświadczenie (Experience):** Wplataj w treść zwroty wskazujące na osobiste doświadczenie, np. "Z mojego doświadczenia...", "Częstym błędem, który obserwuję, jest...".
- **Ekspertyza (Expertise):** Używaj precyzyjnej terminologii.
- **Autorytatywność (Authoritativeness):** Pisz w sposób pewny i zdecydowany.
- **Zaufanie (Trustworthiness):** Bądź transparentny. Jeśli produkt lub metoda ma wady, wspomnij o nich.

# SŁOWA KLUCZOWE
Naturalnie wpleć w treść następujące słowa kluczowe: {{SLOWA_KLUCZOWE}}.
Dodatkowo, wpleć w treść poniższe frazy semantyczne, aby zwiększyć głębię tematyczną: {{DODATKOWE_SLOWA_SEMANTYCZNE}}.

# FORMATOWANIE
Stosuj się ściśle do zasad formatowania HTML podanych w głównym prompcie systemowym."""

def call_gemini(api_key, prompt):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    return model.generate_content(prompt).text

def call_gpt4o_mini(api_key, prompt):
    client = openai.OpenAI(api_key=api_key)
    response = client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}])
    return response.choices[0].message.content

def call_gpt5_nano(api_key, prompt):
    client = openai.OpenAI(api_key=api_key)
    response = client.responses.create(model="gpt-5-nano", input=[{"role": "user", "content": prompt}])
    return response.output_text

def generate_article_two_parts(model_function, api_key, title, prompt):
    part1_text = model_function(api_key, f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\n{prompt}\n\nNapisz PIERWSZĄ POŁOWĘ tego artykułu. Zatrzymaj się w naturalnym miejscu.")
    part2_text = model_function(api_key, f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\nOto pierwsza połowa artykułu. Dokończ go, pisząc drugą połowę. Kontynuuj płynnie. Nie dodawaj wstępów typu 'Oto kontynuacja'.\nOryginalne wytyczne: {prompt}\n---DOTYCHCZAS NAPISANA TREŚĆ---\n{part1_text}")
    return title, part1_text.strip() + "\n\n" + part2_text.strip()

def generate_article_dispatcher(model, api_key, title, prompt):
    try:
        if model == "gemini-1.5-flash": return generate_article_two_parts(lambda k, p: call_gemini(k, p), api_key, title, prompt)
        elif model == "gpt-4o-mini": return generate_article_two_parts(lambda k, p: call_gpt4o_mini(k, p), api_key, title, prompt)
        elif model == "gpt-5-nano": return generate_article_two_parts(lambda k, p: call_gpt5_nano(k, p), api_key, title, prompt)
        else: return title, f"**BŁĄD: Nieznany model '{model}'**"
    except Exception as e:
        if model == "gpt-5-nano" and "has no attribute 'responses'" in str(e): return title, "**BŁĄD (GPT-5):** Biblioteka `openai` nie obsługuje jeszcze API `responses`."
        return title, f"**BŁĄD KRYTYCZNY:** {str(e)}"

def generate_single_brief_gpt5(api_key, topic):
    try:
        prompt = f"""Jesteś strategiem treści SEO. Twoim zadaniem jest stworzenie szczegółowego briefu dla artykułu na temat: "{topic}".
Brief musi być w formacie JSON i zawierać klucze:
- "temat_artykulu": Dokładny, angażujący tytuł.
- "grupa_docelowa": Krótki opis, dla kogo jest artykuł.
- "zagadnienia_kluczowe": Array 3-5 głównych sekcji (nagłówków H2).
- "slowa_kluczowe": Array 5-10 głównych słów kluczowych.
- "dodatkowe_slowa_semantyczne": Array 5-10 fraz i kolokacji semantycznie wspierających główny temat.

Wygeneruj brief JSON dla tematu: "{topic}" """
        json_string = call_gpt5_nano(api_key, prompt).strip().replace("```json", "").replace("```", "")
        return topic, json.loads(json_string)
    except Exception as e:
        return topic, {"error": f"Błąd generowania briefu: {str(e)}"}

def generate_meta_tags_gpt5(api_key, article_title, article_content, keywords):
    try:
        prompt = f"""Jesteś ekspertem SEO copywritingu. Przeanalizuj poniższy artykuł i stwórz do niego idealne meta tagi.
Temat główny: {article_title}
Słowa kluczowe: {", ".join(keywords)}
Treść artykułu (fragment):
{article_content[:2500]}

Zwróć odpowiedź WYŁĄCZNIE w formacie JSON z dwoma kluczami: "meta_title" (max 60 znaków, angażujący, z główną frazą na początku) i "meta_description" (max 155 znaków, zachęcający do kliknięcia, z call-to-action i słowami kluczowymi)."""
        json_string = call_gpt5_nano(api_key, prompt).strip().replace("```json", "").replace("```", "")
        return json.loads(json_string)
    except Exception:
        return {"meta_title": article_title, "meta_description": ""}

def generate_keyword_variations_gpt5(api_key, keywords):
    try:
        prompt = f"""Jesteś lingwistą i ekspertem SEO. Dla podanej listy słów kluczowych, wygeneruj ich najważniejsze odmiany gramatyczne i bliskie synonimy.
Zwróć odpowiedź WYŁĄCZNIE w formacie JSON, gdzie kluczem jest oryginalna fraza, a wartością jest lista 3-4 jej odmian.
Lista słów: {json.dumps(keywords, ensure_ascii=False)}"""
        json_string = call_gpt5_nano(api_key, prompt).strip().replace("```json", "").replace("```", "")
        return json.loads(json_string)
    except Exception:
        return {kw: [kw] for kw in keywords}

def apply_interlinking(content, links_library):
    for link in links_library:
        all_keywords = [link['keyword']] + json.loads(link.get('keyword_variations', '[]'))
        # Używamy seta, aby uniknąć duplikatów i sortujemy od najdłuższej frazy do najkrótszej, aby uniknąć błędów
        unique_keywords = sorted(list(set(all_keywords)), key=len, reverse=True)
        
        for keyword in unique_keywords:
            # Regex, który znajduje słowo, ale nie wewnątrz tagów HTML i nie wewnątrz istniejących linków
            pattern = re.compile(r'\b(' + re.escape(keyword) + r')\b(?![^<]*>|[^<>]*</a)', re.IGNORECASE)
            # Podmień tylko pierwsze znalezione wystąpienie
            if pattern.search(content):
                replacement = f'<a href="{link["url"]}">{keyword}</a>'
                content = pattern.sub(replacement, content, 1)
                break # Przejdź do następnego linku z biblioteki po znalezieniu dopasowania
    return content

# --- INTERFEJS UŻYTKOWNIKA (STREAMLIT) ---

st.set_page_config(layout="wide", page_title="PBN Manager")
st.title("🚀 PBN Manager")
st.caption("Centralne zarządzanie i generowanie treści dla Twojej sieci blogów.")

conn = get_db_connection()

if 'menu_choice' not in st.session_state: st.session_state.menu_choice = "Zarządzanie Stronami"
if 'generated_articles' not in st.session_state: st.session_state.generated_articles = []
if 'generated_briefs' not in st.session_state: st.session_state.generated_briefs = []

st.sidebar.header("Menu Główne")
menu_options = ["Zarządzanie Stronami", "Zarządzanie Personami", "Biblioteka Linkowania", "Generator Briefów", "Generowanie Treści", "Harmonogram Publikacji", "Zarządzanie Treścią", "Dashboard"]
st.session_state.menu_choice = st.sidebar.radio("Wybierz sekcję:", menu_options, key='menu_radio', label_visibility="collapsed")

st.sidebar.header("Konfiguracja API")
MODEL_API_MAP = {"gpt-4o-mini": ("OPENAI_API_KEY", "Klucz OpenAI API"), "gpt-5-nano": ("OPENAI_API_KEY", "Klucz OpenAI API"), "gemini-1.5-flash": ("GOOGLE_API_KEY", "Klucz Google AI API")}
active_model_for_articles = st.session_state.get('selected_model_for_articles', "gpt-5-nano")
active_model_for_briefs = "gpt-5-nano"
active_model = active_model_for_briefs if st.session_state.menu_choice == "Generator Briefów" else active_model_for_articles
api_key_name, api_key_label = MODEL_API_MAP[active_model]
api_key = st.secrets.get(api_key_name)
if not api_key:
    api_key = st.sidebar.text_input(api_key_label, type="password", help=f"Wklej swój klucz {api_key_label}.")

with st.sidebar.expander("Zarządzanie Konfiguracją (Plik JSON)"):
    uploaded_file = st.file_uploader("Załaduj plik konfiguracyjny", type="json", key="config_uploader")
    if uploaded_file is not None:
        if uploaded_file.file_id != st.session_state.get('last_uploaded_file_id'):
            try:
                config_data = json.load(uploaded_file)
                db_execute(conn, "DELETE FROM sites"); db_execute(conn, "DELETE FROM personas"); db_execute(conn, "DELETE FROM interlinks")
                for site in config_data.get('sites', []):
                    encrypted_password_bytes = base64.b64decode(site['app_password_b64'])
                    db_execute(conn, "INSERT INTO sites (name, url, username, app_password) VALUES (?, ?, ?, ?)", (site['name'], site['url'], site['username'], encrypted_password_bytes))
                for persona in config_data.get('personas', []):
                    db_execute(conn, "INSERT INTO personas (name, description) VALUES (?, ?)", (persona['name'], persona['description']))
                for link in config_data.get('interlinks', []):
                    db_execute(conn, "INSERT INTO interlinks (keyword, keyword_variations, url, source_post_id, source_site_id, type) VALUES (?, ?, ?, ?, ?, ?)",
                               (link['keyword'], link['keyword_variations'], link['url'], link.get('source_post_id'), link.get('source_site_id'), link['type']))
                st.session_state.last_uploaded_file_id = uploaded_file.file_id
                st.success(f"Pomyślnie załadowano konfigurację!")
                st.rerun()
            except Exception as e:
                st.error(f"Błąd podczas przetwarzania pliku: {e}")
    
    sites_for_export = db_execute(conn, "SELECT name, url, username, app_password FROM sites", fetch="all")
    personas_for_export = db_execute(conn, "SELECT name, description FROM personas", fetch="all")
    interlinks_for_export = db_execute(conn, "SELECT keyword, keyword_variations, url, source_post_id, source_site_id, type FROM interlinks", fetch="all")
    if sites_for_export or personas_for_export or interlinks_for_export:
        export_data = {'sites': [], 'personas': [], 'interlinks': []}
        for name, url, username, encrypted_pass_bytes in sites_for_export:
            encrypted_pass_b64 = base64.b64encode(encrypted_pass_bytes).decode('utf-8')
            export_data['sites'].append({'name': name, 'url': url, 'username': username, 'app_password_b64': encrypted_pass_b64})
        for name, description in personas_for_export:
            export_data['personas'].append({'name': name, 'description': description})
        for kw, vars, url, p_id, s_id, type in interlinks_for_export:
            export_data['interlinks'].append({'keyword': kw, 'keyword_variations': vars, 'url': url, 'source_post_id': p_id, 'source_site_id': s_id, 'type': type})
        st.download_button(label="Pobierz konfigurację", data=json.dumps(export_data, indent=2), file_name="pbn_config.json", mime="application/json")

# --- WYŚWIETLANIE WYBRANEJ ZAKŁADKI ---

if st.session_state.menu_choice == "Zarządzanie Stronami":
    st.header("Zarządzanie Stronami")
    st.info("W tej sekcji możesz dodać lub usunąć strony WordPress z bieżącej sesji. Pamiętaj, aby zapisać zmiany do pliku konfiguracyjnego w panelu bocznym.")
    st.subheader("Dodaj nową stronę")
    with st.form("add_site_form", clear_on_submit=True):
        name = st.text_input("Przyjazna nazwa strony")
        url = st.text_input("URL strony", placeholder="https://twojastrona.pl")
        username = st.text_input("Login WordPress")
        app_password = st.text_input("Hasło Aplikacji", type="password")
        submitted = st.form_submit_button("Testuj połączenie i Zapisz")
        if submitted:
            if not all([name, url, username, app_password]): st.error("Wszystkie pola są wymagane!")
            else:
                with st.spinner("Testowanie połączenia..."):
                    api = WordPressAPI(url, username, app_password)
                    success, message = api.test_connection()
                if success:
                    encrypted_password = encrypt_data(app_password)
                    try:
                        db_execute(conn, "INSERT INTO sites (name, url, username, app_password) VALUES (?, ?, ?, ?)", (name, url, username, encrypted_password))
                        st.success(f"Strona '{name}' dodana! Pamiętaj, aby zapisać konfigurację do pliku.")
                    except sqlite3.IntegrityError: st.error(f"Strona o URL '{url}' już istnieje w bazie.")
                else: st.error(f"Nie udało się dodać strony. Błąd: {message}")
    st.subheader("Lista załadowanych stron")
    sites = db_execute(conn, "SELECT id, name, url, username FROM sites", fetch="all")
    if not sites: st.info("Brak załadowanych stron.")
    else:
        for site_id, name, url, username in sites:
            cols = st.columns([0.4, 0.4, 0.2])
            cols[0].markdown(f"**{name}**\n\n{url}")
            cols[1].text(f"Login: {username}")
            if cols[2].button("Usuń", key=f"delete_{site_id}"):
                db_execute(conn, "DELETE FROM sites WHERE id = ?", (site_id,))
                st.success(f"Strona '{name}' usunięta! Pamiętaj, aby zapisać nową konfigurację do pliku.")
                st.rerun()

elif st.session_state.menu_choice == "Dashboard":
    st.header("Dashboard")
    sites = db_execute(conn, "SELECT id FROM sites", fetch="all")
    if not sites:
        st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami' lub załaduj plik konfiguracyjny.")
    else:
        if st.button("Odśwież wszystkie statystyki"): st.cache_data.clear()
        @st.cache_data(ttl=600)
        def get_all_stats():
            all_data = []
            sites_for_stats = db_execute(get_db_connection(), "SELECT id, name, url, username, app_password FROM sites", fetch="all")
            progress_bar = st.progress(0, text="Pobieranie danych...")
            for i, (site_id, name, url, username, encrypted_pass) in enumerate(sites_for_stats):
                password = decrypt_data(encrypted_pass)
                api = WordPressAPI(url, username, password)
                stats = api.get_stats()
                all_data.append({"Nazwa": name, "URL": url, "Liczba wpisów": stats['total_posts'], "Ostatni wpis": stats['last_post_date']})
                progress_bar.progress((i + 1) / len(sites_for_stats), text=f"Pobieranie danych dla: {name}")
            progress_bar.empty()
            return all_data
        stats_data = get_all_stats()
        df = pd.DataFrame(stats_data)
        total_posts_sum = pd.to_numeric(df['Liczba wpisów'], errors='coerce').sum()
        col1, col2 = st.columns(2)
        col1.metric("Liczba podłączonych stron", len(sites))
        col2.metric("Łączna liczba wpisów", f"{int(total_posts_sum):,}".replace(",", " "))
        st.dataframe(df, use_container_width=True)

elif st.session_state.menu_choice == "Generator Briefów":
    st.header("📝 Generator Briefów z GPT-5 Nano")
    st.info("Krok 1: Wpisz tematy artykułów (każdy w nowej linii). Aplikacja wygeneruje dla nich szczegółowe briefy.")
    if MODEL_API_MAP[active_model_for_briefs][0] != api_key_name: st.warning(f"Generator briefów używa {active_model_for_briefs}. Upewnij się, że w panelu bocznym jest aktywny i wpisany klucz {MODEL_API_MAP[active_model_for_briefs][1]}.")
    if not api_key: st.error("Wprowadź klucz OpenAI API w panelu bocznym.")
    else:
        topics_input = st.text_area("Wprowadź tematy artykułów (jeden na linię)", height=250)
        if st.button("Generuj briefy", type="primary"):
            topics = [topic.strip() for topic in topics_input.split('\n') if topic.strip()]
            if not topics: st.error("Wpisz przynajmniej jeden temat.")
            else:
                st.session_state.generated_briefs = []
                with st.spinner(f"Generowanie {len(topics)} briefów..."):
                    progress_bar = st.progress(0, text="Oczekiwanie na wyniki...")
                    completed_count = 0
                    with ThreadPoolExecutor(max_workers=10) as executor:
                        futures = {executor.submit(generate_single_brief_gpt5, api_key, topic): topic for topic in topics}
                        for future in as_completed(futures):
                            topic, brief_data = future.result()
                            st.session_state.generated_briefs.append({"topic": topic, "brief": brief_data})
                            completed_count += 1
                            progress_bar.progress(completed_count / len(topics), text=f"Ukończono {completed_count}/{len(topics)}...")
                st.success("Generowanie briefów zakończone!")
    if st.session_state.generated_briefs:
        st.subheader("Wygenerowane Briefy")
        if st.button("Przejdź do generowania artykułów z tych briefów"):
            st.session_state.menu_choice = "Generowanie Treści"
            st.rerun()
        for i, item in enumerate(st.session_state.generated_briefs):
            with st.expander(f"**{i+1}. {item['topic']}**"):
                st.json(item['brief'])

elif st.session_state.menu_choice == "Generowanie Treści":
    st.header("🤖 Generator Treści AI")
    st.info("Krok 2: Wybierz briefy, Personę autora i model AI, a następnie wygeneruj finalne artykuły.")
    if not st.session_state.generated_briefs: st.warning("Brak wygenerowanych briefów. Przejdź najpierw do 'Generator Briefów'.")
    else:
        personas_list = db_execute(conn, "SELECT id, name, description FROM personas", fetch="all")
        persona_map = {name: description for id, name, description in personas_list}
        if not persona_map: st.error("Brak zdefiniowanych Person. Przejdź do 'Zarządzanie Personami', aby dodać pierwszą.")
        else:
            col1, col2 = st.columns(2)
            selected_persona_name = col1.selectbox("Wybierz Personę autora", options=list(persona_map.keys()))
            selected_model = col2.selectbox("Wybierz model do generowania artykułów", options=list(MODEL_API_MAP.keys()), key='selected_model_for_articles', index=1)
            if MODEL_API_MAP[selected_model][0] != api_key_name: st.warning(f"Wybrany model wymaga klucza {MODEL_API_MAP[selected_model][1]}. Upewnij się, że jest aktywny i wpisany w panelu bocznym.")
            if not api_key: st.error(f"Wprowadź swój {api_key_label} w panelu bocznym.")
            else:
                df = pd.DataFrame(st.session_state.generated_briefs)
                df['Zaznacz'] = False; df['Temat'] = df['topic']; df['Brief'] = df['brief'].apply(lambda x: json.dumps(x, ensure_ascii=False, indent=2))
                with st.form("article_generation_form"):
                    st.subheader("Wybierz briefy do przetworzenia")
                    edited_df = st.data_editor(df[['Zaznacz', 'Temat', 'Brief']], hide_index=True, use_container_width=True)
                    submitted = st.form_submit_button("Generuj zaznaczone artykuły", type="primary")
                    if submitted:
                        selected_briefs = edited_df[edited_df.Zaznacz]
                        if selected_briefs.empty: st.error("Zaznacz przynajmniej jeden brief.")
                        else:
                            tasks_to_run = []
                            for index, row in selected_briefs.iterrows():
                                brief_data = json.loads(row['Brief'])
                                if 'error' in brief_data: continue
                                final_prompt = MASTER_PROMPT_TEMPLATE.replace("{{PERSONA_DESCRIPTION}}", persona_map[selected_persona_name])
                                final_prompt = final_prompt.replace("{{TEMAT_ARTYKULU}}", brief_data.get("temat_artykulu", row["Temat"]))
                                final_prompt = final_prompt.replace("{{GRUPA_DOCELOWA}}", brief_data.get("grupa_docelowa", ""))
                                final_prompt = final_prompt.replace("{{SLOWA_KLUCZOWE}}", ", ".join(brief_data.get("slowa_kluczowe", [])))
                                final_prompt = final_prompt.replace("{{DODATKOWE_SLOWA_SEMANTYCZNE}}", ", ".join(brief_data.get("dodatkowe_slowa_semantyczne", [])))
                                zagadnienia_str = "\n".join([f"- {z}" for z in brief_data.get("zagadnienia_kluczowe", [])])
                                final_prompt = final_prompt.replace("{{ZAGADNIENIA_KLUCZOWE}}", zagadnienia_str)
                                tasks_to_run.append({'title': brief_data.get("temat_artykulu", row["Temat"]), 'prompt': final_prompt, 'keywords': brief_data.get("slowa_kluczowe", [])})
                            st.session_state.generated_articles = []
                            with st.spinner(f"Generowanie {len(tasks_to_run)} artykułów..."):
                                progress_bar = st.progress(0)
                                completed_count = 0
                                with ThreadPoolExecutor(max_workers=10) as executor:
                                    future_to_task = {executor.submit(generate_article_dispatcher, selected_model, api_key, task['title'], task['prompt']): task for task in tasks_to_run}
                                    for future in as_completed(future_to_task):
                                        task = future_to_task[future]
                                        title, content = future.result()
                                        st.info(f"Generowanie meta tagów dla: {title}...")
                                        meta_tags = generate_meta_tags_gpt5(api_key, title, content, task['keywords'])
                                        st.session_state.generated_articles.append({"title": title, "content": content, **meta_tags})
                                        completed_count += 1
                                        progress_bar.progress(completed_count / len(tasks_to_run), text=f"Ukończono {completed_count}/{len(tasks_to_run)}...")
                            st.success("Generowanie artykułów zakończone!")
                            st.session_state.menu_choice = "Harmonogram Publikacji"
                            st.rerun()

elif st.session_state.menu_choice == "Zarządzanie Personami":
    st.header("🎭 Zarządzanie Personami")
    st.info("Persona to opis autora, który jest wstrzykiwany do głównego promptu, aby nadać artykułom unikalny styl i ton.")
    with st.expander("Dodaj nową Personę", expanded=True):
        with st.form("add_persona_form", clear_on_submit=True):
            persona_name = st.text_input("Nazwa Persony (np. 'Dietetyk Kliniczny', 'Inżynier Oprogramowania')")
            persona_desc = st.text_area("Opis Persony", height=150, help="Opisz kim jest autor, jakie ma doświadczenie i styl. Np. 'Jesteś doświadczonym dietetykiem klinicznym z 15-letnią praktyką, piszącym w sposób empatyczny i oparty na dowodach naukowych.'")
            submitted = st.form_submit_button("Zapisz Personę")
            if submitted:
                if persona_name and persona_desc:
                    try:
                        db_execute(conn, "INSERT INTO personas (name, description) VALUES (?, ?)", (persona_name, persona_desc))
                        st.success(f"Persona '{persona_name}' została zapisana! Pamiętaj, aby zapisać całą konfigurację do pliku.")
                    except sqlite3.IntegrityError:
                        st.error(f"Persona o nazwie '{persona_name}' już istnieje.")
                else:
                    st.error("Nazwa i opis Persony nie mogą być puste.")
    st.subheader("Lista zapisanych Person")
    personas = db_execute(conn, "SELECT id, name, description FROM personas", fetch="all")
    if not personas:
        st.info("Brak zapisanych Person. Dodaj swoją pierwszą, używając formularza powyżej.")
    else:
        for id, name, desc in personas:
            with st.expander(f"**{name}**"):
                st.text_area("Opis", value=desc, height=100, disabled=True, key=f"desc_{id}")
                if st.button("Usuń Personę", key=f"delete_persona_{id}"):
                    db_execute(conn, "DELETE FROM personas WHERE id = ?", (id,))
                    st.success(f"Persona '{name}' usunięta! Pamiętaj, aby zapisać konfigurację.")
                    st.rerun()

elif st.session_state.menu_choice == "Biblioteka Linkowania":
    st.header("🔗 Biblioteka Linkowania")
    st.info("Zarządzaj linkami, które będą automatycznie wstawiane do nowych artykułów. Linki typu 'Auto' są dodawane automatycznie po każdej udanej publikacji.")
    with st.expander("Dodaj nowy link ręcznie"):
        with st.form("add_link_form", clear_on_submit=True):
            keyword = st.text_input("Słowo kluczowe (anchor text)")
            url = st.text_input("Docelowy URL")
            submitted = st.form_submit_button("Zapisz link")
            if submitted and keyword and url:
                try:
                    db_execute(conn, "INSERT INTO interlinks (keyword, url, type) VALUES (?, ?, ?)", (keyword, url, 'manual'))
                    st.success("Link dodany ręcznie!")
                except sqlite3.IntegrityError:
                    st.error("Link z takim słowem kluczowym już istnieje.")
    st.subheader("Zapisane linki")
    links_data = db_execute(conn, "SELECT id, keyword, url, type FROM interlinks", fetch="all")
    if not links_data:
        st.info("Biblioteka jest pusta. Opublikuj artykuły z włączoną opcją 'Dodaj do biblioteki' lub dodaj link ręcznie.")
    else:
        df_links = pd.DataFrame(links_data, columns=['ID', 'Słowo kluczowe', 'URL', 'Typ'])
        st.dataframe(df_links, hide_index=True)

elif st.session_state.menu_choice == "Harmonogram Publikacji":
    st.header("Harmonogram Publikacji")
    st.info("Krok 3: Wybierz artykuły, ustawienia publikacji i zaplanuj je z rozłożeniem w czasie.")
    if not st.session_state.generated_articles:
        st.warning("Brak wygenerowanych artykułów. Przejdź do 'Generator Briefów', a następnie 'Generowanie Treści'.")
    else:
        sites = db_execute(conn, "SELECT id, name FROM sites", fetch="all")
        site_options = {name: site_id for site_id, name in sites}
        if not site_options: st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami'.")
        else:
            df = pd.DataFrame(st.session_state.generated_articles)
            df['Zaznacz'] = True
            with st.form("bulk_schedule_form"):
                st.subheader("1. Wybierz artykuły do publikacji")
                edited_df = st.data_editor(df[['Zaznacz', 'title', 'meta_title', 'meta_description']], hide_index=True, use_container_width=True,
                                           column_config={"title": "Tytuł Artykułu", "meta_title": "Meta Tytuł", "meta_description": "Meta Opis"})
                st.subheader("2. Ustawienia publikacji")
                selected_sites_names = st.multiselect("Wybierz strony docelowe", options=site_options.keys())
                categories_str = st.text_input("Kategorie (wspólne dla wszystkich, oddzielone przecinkami)")
                tags_str = st.text_input("Tagi (wspólne dla wszystkich, oddzielone przecinkami)")
                featured_image_url = st.text_input("URL obrazka wyróżniającego (wspólny dla wszystkich)")
                
                st.subheader("3. Ustawienia Linkowania")
                enable_interlinking = st.checkbox("Włącz automatyczne linkowanie dla tej publikacji", value=True)
                add_to_library = st.checkbox("Dodaj opublikowane artykuły do biblioteki linkowania", value=True)

                st.subheader("4. Planowanie w czasie (Staggering)")
                col_date1, col_date2, col_date3 = st.columns(3)
                start_date = col_date1.date_input("Data publikacji pierwszego artykułu", min_value=datetime.now())
                start_time = col_date2.time_input("Godzina publikacji pierwszego artykułu")
                interval_hours = col_date3.number_input("Odstęp między publikacjami (w godzinach)", min_value=1, value=8)
                
                submitted = st.form_submit_button("Zaplanuj zaznaczone artykuły", type="primary")
                if submitted:
                    selected_articles = edited_df[edited_df.Zaznacz]
                    if selected_articles.empty or not selected_sites_names:
                        st.error("Zaznacz przynajmniej jeden artykuł i jedną stronę docelową.")
                    else:
                        current_publish_time = datetime.combine(start_date, start_time)
                        links_library = db_execute(conn, "SELECT keyword, keyword_variations, url FROM interlinks", fetch="all")
                        links_library_dicts = [{'keyword': r[0], 'keyword_variations': r[1], 'url': r[2]} for r in links_library]

                        with st.spinner("Planowanie publikacji..."):
                            for index, row in selected_articles.iterrows():
                                full_article_data = st.session_state.generated_articles[index]
                                content_to_publish = full_article_data['content']
                                
                                if enable_interlinking:
                                    st.info(f"Stosowanie linkowania wewnętrznego dla: '{row['title']}'...")
                                    content_to_publish = apply_interlinking(content_to_publish, links_library_dicts)

                                for site_name in selected_sites_names:
                                    site_id = site_options[site_name]
                                    site_info = db_execute(conn, "SELECT url, username, app_password FROM sites WHERE id = ?", (site_id,), fetch="one")
                                    url, username, encrypted_pass = site_info
                                    password = decrypt_data(encrypted_pass)
                                    api = WordPressAPI(url, username, password)
                                    available_categories = api.get_categories()
                                    target_category_ids = []
                                    if categories_str:
                                        input_categories = [cat.strip() for cat in categories_str.split(',')]
                                        for cat_name in input_categories:
                                            if cat_name in available_categories: target_category_ids.append(available_categories[cat_name])
                                            else: st.warning(f"Na stronie '{site_name}' nie znaleziono kategorii '{cat_name}'.")
                                    target_tags = [tag.strip() for tag in tags_str.split(',')] if tags_str else []
                                    
                                    st.info(f"Planowanie '{row['title']}' na {site_name} na dzień {current_publish_time.strftime('%Y-%m-%d %H:%M')}...")
                                    success, message, new_post_url = api.publish_post(
                                        row['title'], content_to_publish, "future", current_publish_time.isoformat(),
                                        target_category_ids, target_tags, featured_image_url=featured_image_url,
                                        meta_title=row['meta_title'], meta_description=row['meta_description']
                                    )
                                    if success:
                                        st.success(f"[{site_name}]: {message}")
                                        if add_to_library and new_post_url:
                                            st.info(f"Dodawanie do biblioteki linkowania: {new_post_url}")
                                            brief_data = st.session_state.generated_briefs[index]['brief']
                                            keywords_from_brief = brief_data.get('slowa_kluczowe', [])
                                            if keywords_from_brief:
                                                variations = generate_keyword_variations_gpt5(api_key, keywords_from_brief)
                                                for kw, vars_list in variations.items():
                                                    try:
                                                        db_execute(conn, "INSERT INTO interlinks (keyword, keyword_variations, url, source_site_id, type) VALUES (?, ?, ?, ?, ?)",
                                                                   (kw, json.dumps(vars_list), new_post_url, site_id, 'auto'))
                                                    except sqlite3.IntegrityError:
                                                        pass # Ignoruj, jeśli słowo kluczowe już istnieje
                                    else:
                                        st.error(f"[{site_name}]: {message}")
                                current_publish_time += timedelta(hours=interval_hours)
                        st.success("Zakończono planowanie wszystkich zaznaczonych artykułów!")

elif st.session_state.menu_choice == "Zarządzanie Treścią":
    st.header("Zarządzanie Treścią i Masowa Edycja")
    sites = db_execute(conn, "SELECT id, name, url, username, app_password FROM sites", fetch="all")
    site_options = {site[1]: site for site in sites}
    if not site_options: st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami'.")
    else:
        selected_site_name = st.selectbox("Wybierz stronę do edycji", options=site_options.keys())
        if selected_site_name:
            site_id, name, url, username, encrypted_pass = site_options[selected_site_name]
            password = decrypt_data(encrypted_pass)
            api = WordPressAPI(url, username, password)
            st.subheader(f"Wpisy na stronie: {name}")
            @st.cache_data(ttl=300)
            def get_site_data(_url, _username, _password):
                api_instance = WordPressAPI(_url, _username, _password)
                posts = api_instance.get_posts()
                categories = api_instance.get_categories()
                all_users = api_instance.get_users()
                return posts, categories, all_users
            posts, categories, all_users = get_site_data(url, username, password)
            users_from_posts = {post['author_name']: post['author_id'] for post in posts if post.get('author_name') != 'N/A'} if posts else {}
            final_users_map = {**all_users, **users_from_posts}
            if not posts: st.info("Nie znaleziono wpisów na tej stronie lub wystąpił błąd połączenia.")
            else:
                df = pd.DataFrame(posts).rename(columns={'author_name': 'author'})
                df['Zaznacz'] = False
                st.info("Zaznacz wpisy, które chcesz edytować, a następnie użyj formularza masowej edycji poniżej.")
                edited_df = st.data_editor(df, column_config={"Zaznacz": st.column_config.CheckboxColumn(required=True)},
                                           disabled=["id", "title", "date", "author", "categories", "author_id"], hide_index=True, use_container_width=True)
                selected_posts = edited_df[edited_df.Zaznacz]
                if not selected_posts.empty:
                    st.subheader(f"Masowa edycja dla {len(selected_posts)} zaznaczonych wpisów")
                    with st.form("bulk_edit_form"):
                        new_category_names = st.multiselect("Zastąp kategorie", options=categories.keys())
                        new_author_name = st.selectbox("Zmień autora", options=[None] + sorted(list(final_users_map.keys())))
                        submitted = st.form_submit_button("Wykonaj masową edycję")
                        if submitted:
                            if not new_category_names and not new_author_name: st.error("Wybierz przynajmniej jedną akcję do wykonania.")
                            else:
                                update_data = {}
                                if new_category_names: update_data['categories'] = [categories[name] for name in new_category_names]
                                if new_author_name: update_data['author'] = final_users_map[new_author_name]
                                with st.spinner("Aktualizowanie wpisów..."):
                                    progress_bar = st.progress(0)
                                    total_selected = len(selected_posts)
                                    for i, post_id in enumerate(selected_posts['id']):
                                        success, message = api.update_post(post_id, update_data)
                                        if success: st.success(message)
                                        else: st.error(message)
                                        progress_bar.progress((i + 1) / total_selected)
                                st.info("Proces zakończony. Odśwież dane, aby zobaczyć zmiany.")
                                st.cache_data.clear()
                else:
                    st.caption("Zaznacz przynajmniej jeden wpis, aby aktywować panel masowej edycji.")
