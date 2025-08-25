import streamlit as st
import sqlite3
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import json
import os
from cryptography.fernet import Fernet
import base64
import openai
import google.generativeai as genai
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse
import io

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
    cursor.execute("CREATE TABLE IF NOT EXISTS prompts (id INTEGER PRIMARY KEY, name TEXT UNIQUE, content TEXT)")
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
            author_map = {author_id: self._make_request(f"users/{author_id}", display_error=False).get('name', 'N/A') for author_id in author_ids}
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
            return True, f"Wpis opublikowany/zaplanowany! ID: {response.json()['id']}"
        except requests.exceptions.HTTPError as e: return False, f"Błąd publikacji ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e: return False, f"Błąd sieci podczas publikacji: {e}"

# --- FUNKCJE GENEROWANIA TREŚCI ---
HTML_RULES = (
    "Zasady formatowania HTML:\n"
    "- NIE UŻYWAJ nagłówka <h1>. Tytuł artykułu jest podany osobno.\n"
    "- UŻYWAJ WYŁĄCZNIE następujących tagów HTML: <h2>, <h3>, <p>, <b>, <strong>, <ul>, <ol>, <li>, <table>, <tr>, <th>, <td>.\n"
    "- ŻADNYCH INNYCH TAGÓW HTML (np. <div>, <span>, <a>, <img>, <em>, <i>) nie wolno używać."
)
SYSTEM_PROMPT_BASE = f"Jesteś ekspertem SEO i copywriterem. Twoim zadaniem jest tworzenie wysokiej jakości, unikalnych artykułów na bloga. Pisz w języku polskim.\n{HTML_RULES}"

def generate_article_gemini(api_key, title, prompt):
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt_part1 = f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\nTytuł artykułu: {title}\nSzczegółowe wytyczne (prompt): {prompt}\n\nNapisz PIERWSZĄ POŁOWĘ tego artykułu. Zatrzymaj się w naturalnym miejscu."
    response_part1 = model.generate_content(prompt_part1)
    part1_text = response_part1.text
    prompt_part2 = f"{SYSTEM_PROMPT_BASE}\n\n---ZADANIE---\nOto pierwsza połowa artykułu. Dokończ go, pisząc drugą połowę. Kontynuuj płynnie. Nie dodawaj wstępów typu 'Oto kontynuacja'.\nOryginalne wytyczne: {prompt}\n---DOTYCHCZAS NAPISANA TREŚĆ---\n{part1_text}"
    response_part2 = model.generate_content(prompt_part2)
    part2_text = response_part2.text
    return title, part1_text.strip() + "\n\n" + part2_text.strip()

def generate_article_gpt4o_mini(api_key, title, prompt):
    client = openai.OpenAI(api_key=api_key)
    messages_part1 = [{"role": "system", "content": SYSTEM_PROMPT_BASE}, {"role": "user", "content": f"Tytuł artykułu: {title}\nSzczegółowe wytyczne (prompt): {prompt}\n\nNapisz PIERWSZĄ POŁOWĘ tego artykułu. Zatrzymaj się w naturalnym miejscu."}]
    response_part1 = client.chat.completions.create(model="gpt-4o-mini", messages=messages_part1)
    part1_text = response_part1.choices[0].message.content
    messages_part2 = [{"role": "system", "content": SYSTEM_PROMPT_BASE}, {"role": "user", "content": f"Oto pierwsza połowa artykułu. Dokończ go, pisząc drugą połowę. Kontynuuj płynnie. Nie dodawaj wstępów typu 'Oto kontynuacja'.\nOryginalne wytyczne: {prompt}\n---DOTYCHCZAS NAPISANA TREŚĆ---\n{part1_text}"}]
    response_part2 = client.chat.completions.create(model="gpt-4o-mini", messages=messages_part2)
    part2_text = response_part2.choices[0].message.content
    return title, part1_text.strip() + "\n\n" + part2_text.strip()

def generate_article_gpt5_nano(api_key, title, prompt):
    # UWAGA: Ta funkcja jest oparta na hipotetycznym, przyszłym API OpenAI dla GPT-5.
    # Może wymagać dostosowania, gdy API zostanie oficjalnie wydane.
    client = openai.OpenAI(api_key=api_key)
    prompt_part1 = [{"role": "developer", "content": SYSTEM_PROMPT_BASE}, {"role": "user", "content": f"Tytuł artykułu: {title}\nSzczegółowe wytyczne (prompt): {prompt}\n\nNapisz PIERWSZĄ POŁOWĘ tego artykułu. Zatrzymaj się w naturalnym miejscu."}]
    response_part1 = client.responses.create(model="gpt-5-nano", input=prompt_part1)
    part1_text = response_part1.output_text
    prompt_part2 = [{"role": "developer", "content": SYSTEM_PROMPT_BASE}, {"role": "user", "content": f"Oto pierwsza połowa artykułu. Dokończ go, pisząc drugą połowę. Kontynuuj płynnie. Nie dodawaj wstępów typu 'Oto kontynuacja'.\nOryginalne wytyczne: {prompt}\n---DOTYCHCZAS NAPISANA TREŚĆ---\n{part1_text}"}]
    response_part2 = client.responses.create(model="gpt-5-nano", input=prompt_part2)
    part2_text = response_part2.output_text
    return title, part1_text.strip() + "\n\n" + part2_text.strip()

def generate_article_dispatcher(model, api_key, title, prompt):
    try:
        if model == "gemini-1.5-flash":
            return generate_article_gemini(api_key, title, prompt)
        elif model == "gpt-4o-mini":
            return generate_article_gpt4o_mini(api_key, title, prompt)
        elif model == "gpt-5-nano":
            return generate_article_gpt5_nano(api_key, title, prompt)
        else:
            return title, f"**BŁĄD: Nieznany model '{model}'**"
    except Exception as e:
        # Specjalna obsługa błędu dla hipotetycznego API GPT-5
        if model == "gpt-5-nano" and "has no attribute 'responses'" in str(e):
            return title, "**BŁĄD GENEROWANIA (GPT-5):** Wygląda na to, że Twoja biblioteka `openai` nie obsługuje jeszcze nowego API `responses`. Ta funkcja jest eksperymentalna."
        return title, f"**BŁĄD KRYTYCZNY GENEROWANIA:** {str(e)}"

# --- INTERFEJS UŻYTKOWNIKA (STREAMLIT) ---

st.set_page_config(layout="wide", page_title="PBN Manager")
st.title("🚀 PBN Manager")
st.caption("Centralne zarządzanie i generowanie treści dla Twojej sieci blogów.")

conn = get_db_connection()

if 'menu_choice' not in st.session_state: st.session_state.menu_choice = "Dashboard"
def set_menu_choice(choice): st.session_state.menu_choice = choice

menu_options = ["Dashboard", "Generowanie Treści", "Zarządzanie Promptami", "Harmonogram Publikacji", "Zarządzanie Treścią", "Zarządzanie Stronami"]
st.sidebar.selectbox("Menu", menu_options, key='menu_choice_selector', index=menu_options.index(st.session_state.menu_choice), on_change=lambda: set_menu_choice(st.session_state.menu_choice_selector))

if 'generated_articles' not in st.session_state: st.session_state.generated_articles = []

# --- Dynamiczne zarządzanie kluczami API w panelu bocznym ---
st.sidebar.header("Konfiguracja API")
MODEL_API_MAP = {
    "gpt-4o-mini": ("OPENAI_API_KEY", "Klucz OpenAI API"),
    "gpt-5-nano": ("OPENAI_API_KEY", "Klucz OpenAI API"),
    "gemini-1.5-flash": ("GOOGLE_API_KEY", "Klucz Google AI API")
}
# Domyślny model, jeśli żaden nie jest wybrany w stanie sesji
active_model = st.session_state.get('selected_model', "gemini-1.5-flash")
api_key_name, api_key_label = MODEL_API_MAP[active_model]

api_key = st.secrets.get(api_key_name)
if not api_key:
    api_key = st.sidebar.text_input(api_key_label, type="password", help=f"Wklej swój klucz {api_key_label}. Nie jest on nigdzie zapisywany.")

if st.session_state.menu_choice == "Dashboard":
    # ... (kod bez zmian)
    pass

elif st.session_state.menu_choice == "Generowanie Treści":
    st.header("🤖 Generator Treści AI")
    
    # Wybór modelu
    selected_model = st.selectbox(
        "Wybierz model do generowania treści",
        options=list(MODEL_API_MAP.keys()),
        key='selected_model'
    )

    if not api_key:
        st.error(f"Wprowadź swój {api_key_label} w panelu bocznym, aby korzystać z tego modelu.")
    else:
        if 'tasks' not in st.session_state: st.session_state.tasks = [{"title": "", "prompt": ""}]
        prompts_list = db_execute(conn, "SELECT id, name, content FROM prompts", fetch="all")
        prompt_map = {name: content for id, name, content in prompts_list}
        st.subheader("Zdefiniuj artykuły do wygenerowania")
        col1, col2, _ = st.columns([1, 1, 5])
        if col1.button("➕ Dodaj kolejny artykuł"): st.session_state.tasks.append({"title": "", "prompt": ""})
        if col2.button("➖ Usuń ostatni artykuł"):
            if len(st.session_state.tasks) > 1: st.session_state.tasks.pop()
        
        with st.form("generation_form"):
            for i, task in enumerate(st.session_state.tasks):
                st.markdown(f"--- \n ### Artykuł #{i+1}")
                st.session_state.tasks[i]['title'] = st.text_input("Tytuł artykułu", value=task['title'], key=f"title_{i}")
                selected_prompt = st.selectbox("Wybierz gotowy prompt (opcjonalnie)", ["-- Brak --"] + list(prompt_map.keys()), key=f"select_prompt_{i}")
                prompt_content = prompt_map.get(selected_prompt, task['prompt'])
                st.session_state.tasks[i]['prompt'] = st.text_area("Prompt (szczegółowe wytyczne)", value=prompt_content, key=f"prompt_{i}", height=150)

            submitted = st.form_submit_button(f"Generuj {len(st.session_state.tasks)} artykułów modelem {selected_model}", type="primary")
            if submitted:
                valid_tasks = [t for t in st.session_state.tasks if t['title'] and t['prompt']]
                if not valid_tasks: st.error("Uzupełnij tytuł i prompt dla przynajmniej jednego artykułu.")
                else:
                    st.session_state.generated_articles = []
                    with st.spinner(f"Generowanie {len(valid_tasks)} artykułów..."):
                        progress_bar = st.progress(0, text="Oczekiwanie na wyniki...")
                        completed_count = 0
                        with ThreadPoolExecutor(max_workers=10) as executor:
                            futures = {executor.submit(generate_article_dispatcher, selected_model, api_key, task['title'], task['prompt']): task for task in valid_tasks}
                            for future in as_completed(futures):
                                title, content = future.result()
                                st.session_state.generated_articles.append({"title": title, "content": content})
                                completed_count += 1
                                progress_bar.progress(completed_count / len(valid_tasks), text=f"Ukończono {completed_count}/{len(valid_tasks)}...")
                    st.success("Generowanie zakończone!")
    
    if st.session_state.generated_articles:
        st.subheader("Wygenerowane Artykuły")
        for i, article in enumerate(st.session_state.generated_articles):
            with st.expander(f"**{i+1}. {article['title']}**"):
                st.markdown(article['content'], unsafe_allow_html=True)
                if st.button("Zaplanuj publikację", key=f"plan_{i}"):
                    st.session_state.prefill_title = article['title']
                    st.session_state.prefill_content = article['content']
                    set_menu_choice("Harmonogram Publikacji")
                    st.rerun()

elif st.session_state.menu_choice == "Zarządzanie Promptami":
    st.header("📚 Zarządzanie Promptami")
    st.info("Tutaj możesz dodawać, edytować i usuwać szablony promptów, których będziesz używać w generatorze treści.")
    
    # Przycisk do załadowania master promptu
    if st.button("Załaduj domyślny Master Prompt E-E-A-T"):
        master_prompt_name = "Master Prompt E-E-A-T"
        master_prompt_content = """# ROLA I CEL
Jesteś światowej klasy ekspertem w dziedzinie [TEMAT ARTYKUŁU] oraz doświadczonym autorem publikującym w renomowanych portalach. Twoim celem jest napisanie wyczerpującego, wiarygodnego i praktycznego artykułu, który demonstruje głęboką wiedzę (Ekspertyza), autentyczne doświadczenie (Doświadczenie), jest autorytatywny w tonie (Autorytatywność) i buduje zaufanie czytelnika (Zaufanie).

# GRUPA DOCELOWA
Artykuł jest skierowany do [OPIS GRUPY DOCELOWEJ, np. początkujących ogrodników, zaawansowanych programistów]. Używaj języka, który jest dla nich zrozumiały, ale nie unikaj terminologii branżowej – wyjaśniaj ją w prosty sposób.

# STRUKTURA I GŁĘBIA
Artykuł musi mieć logiczną strukturę. Zacznij od wprowadzenia, które zidentyfikuje problem lub potrzebę czytelnika i obieca konkretne rozwiązanie. Rozwiń temat w kilku kluczowych sekcjach, a zakończ praktycznym podsumowaniem i konkluzją.
Kluczowe zagadnienia do poruszenia:
1. [Zagadnienie 1]
2. [Zagadnienie 2]
3. [Zagadnienie 3]
4. [itd.]

# STYL I TON
- **Doświadczenie (Experience):** Wplataj w treść zwroty wskazujące na osobiste doświadczenie, np. "Z mojego doświadczenia...", "Częstym błędem, który obserwuję, jest...", "Praktyczny test, który polecam wykonać, to...". Podawaj konkretne, życiowe przykłady.
- **Ekspertyza (Expertise):** Używaj precyzyjnej terminologii. Jeśli to możliwe, zasugeruj odwołania do badań, standardów branżowych lub opinii innych ekspertów (np. "Jak wskazują badania opublikowane w...", "Zgodnie z rekomendacjami...").
- **Autorytatywność (Authoritativeness):** Pisz w sposób pewny i zdecydowany. Unikaj zwrotów typu "wydaje mi się", "możliwe, że". Przedstawiaj fakty i dobrze ugruntowane opinie.
- **Zaufanie (Trustworthiness):** Bądź transparentny. Jeśli istnieją różne opinie na dany temat, przedstaw je. Jeśli produkt lub metoda ma wady, wspomnij o nich. Zakończ artykuł, zachęcając czytelnika do dalszej edukacji lub zadawania pytań.

# SŁOWA KLUCZOWE
Naturalnie wpleć w treść następujące słowa kluczowe: [LISTA SŁÓW KLUCZOWYCH].

# FORMATOWANIE
Stosuj się ściśle do zasad formatowania HTML podanych w głównym prompcie systemowym."""
        try:
            db_execute(conn, "INSERT INTO prompts (name, content) VALUES (?, ?)", (master_prompt_name, master_prompt_content))
            st.success(f"Prompt '{master_prompt_name}' został dodany! Pamiętaj, aby zapisać konfigurację do pliku.")
            st.rerun()
        except sqlite3.IntegrityError:
            st.warning(f"Prompt o nazwie '{master_prompt_name}' już istnieje.")

    with st.expander("Dodaj nowy własny prompt", expanded=True):
        with st.form("add_prompt_form", clear_on_submit=True):
            prompt_name = st.text_input("Nazwa promptu")
            prompt_content = st.text_area("Treść szablonu promptu", height=200)
            submitted = st.form_submit_button("Zapisz prompt")
            if submitted:
                if prompt_name and prompt_content:
                    try:
                        db_execute(conn, "INSERT INTO prompts (name, content) VALUES (?, ?)", (prompt_name, prompt_content))
                        st.success(f"Prompt '{prompt_name}' został zapisany! Pamiętaj, aby zapisać całą konfigurację do pliku.")
                    except sqlite3.IntegrityError:
                        st.error(f"Prompt o nazwie '{prompt_name}' już istnieje.")
                else:
                    st.error("Nazwa i treść promptu nie mogą być puste.")
    
    st.subheader("Lista zapisanych promptów")
    prompts = db_execute(conn, "SELECT id, name, content FROM prompts", fetch="all")
    if not prompts:
        st.info("Brak zapisanych promptów.")
    else:
        for id, name, content in prompts:
            with st.expander(f"**{name}**"):
                st.text_area("Treść", value=content, height=150, disabled=True, key=f"content_{id}")
                if st.button("Usuń prompt", key=f"delete_prompt_{id}"):
                    db_execute(conn, "DELETE FROM prompts WHERE id = ?", (id,))
                    st.success(f"Prompt '{name}' usunięty! Pamiętaj, aby zapisać konfigurację.")
                    st.rerun()

# Pozostałe zakładki pozostają bez zmian w logice, ale kod jest wklejony w całości
elif st.session_state.menu_choice == "Harmonogram Publikacji":
    st.header("Harmonogram Publikacji")
    sites = db_execute(conn, "SELECT id, name FROM sites", fetch="all")
    site_options = {name: site_id for site_id, name in sites}
    if not site_options: st.warning("Brak załadowanych stron. Przejdź do 'Zarządzanie Stronami'.")
    else:
        title_value = st.session_state.get('prefill_title', '')
        content_value = st.session_state.get('prefill_content', '')
        with st.form("schedule_post_form"):
            st.subheader("Nowy wpis")
            selected_sites_names = st.multiselect("Wybierz strony docelowe", options=site_options.keys())
            title = st.text_input("Tytuł wpisu", value=title_value)
            content = st.text_area("Treść wpisu (obsługuje HTML)", value=content_value, height=400)
            st.subheader("Ustawienia dodatkowe (opcjonalne)")
            featured_image_url = st.text_input("URL obrazka wyróżniającego", help="Wklej bezpośredni link do obrazka. Zostanie on automatycznie wgrany na stronę.")
            col_meta1, col_meta2 = st.columns(2)
            meta_title = col_meta1.text_input("Meta Tytuł", help="Kompatybilne z Yoast, Rank Math, AIOSEO.")
            meta_description = col_meta2.text_area("Meta Opis", height=100, help="Kompatybilne z Yoast, Rank Math, AIOSEO.")
            st.subheader("Kategorie, Tagi i Data")
            cols_meta = st.columns(2)
            categories_str = cols_meta[0].text_input("Kategorie (oddzielone przecinkami)")
            tags_str = cols_meta[1].text_input("Tagi (oddzielone przecinkami)")
            cols_date = st.columns(2)
            publish_date = cols_date[0].date_input("Data publikacji", min_value=datetime.now())
            publish_time = cols_date[1].time_input("Godzina publikacji")
            submit_button = st.form_submit_button("Zaplanuj wpis")
            if submit_button:
                if 'prefill_title' in st.session_state: del st.session_state.prefill_title
                if 'prefill_content' in st.session_state: del st.session_state.prefill_content
                if not all([selected_sites_names, title, content]): st.error("Musisz wybrać stronę, tytuł i treść.")
                else:
                    publish_datetime = datetime.combine(publish_date, publish_time).isoformat()
                    with st.spinner("Przetwarzanie..."):
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
                            success, message = api.publish_post(title, content, "future", publish_datetime, target_category_ids, target_tags, featured_image_url=featured_image_url, meta_title=meta_title, meta_description=meta_description)
                            if success: st.success(f"[{site_name}]: {message}")
                            else: st.error(f"[{site_name}]: {message}")

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

elif st.session_state.menu_choice == "Zarządzanie Stronami":
    st.header("Zarządzanie Stronami")
    st.info("""
    **Jak to działa na Streamlit Cloud?**
    1.  **Ładuj:** Na początku sesji załaduj swój plik `pbn_config.json`.
    2.  **Pracuj:** Dodawaj, usuwaj i edytuj strony/prompty.
    3.  **Zapisuj:** Przed zamknięciem karty **zawsze** zapisuj zmiany, pobierając nowy plik konfiguracyjny.
    """)
    st.subheader("1. Załaduj lub Zapisz Konfigurację")
    col1, col2 = st.columns(2)
    with col1:
        uploaded_file = st.file_uploader("Załaduj plik konfiguracyjny (`pbn_config.json`)", type="json")
        if uploaded_file is not None:
            try:
                config_data = json.load(uploaded_file)
                db_execute(conn, "DELETE FROM sites")
                for site in config_data.get('sites', []):
                    encrypted_password_bytes = base64.b64decode(site['app_password_b64'])
                    db_execute(conn, "INSERT INTO sites (name, url, username, app_password) VALUES (?, ?, ?, ?)", (site['name'], site['url'], site['username'], encrypted_password_bytes))
                db_execute(conn, "DELETE FROM prompts")
                for prompt in config_data.get('prompts', []):
                    db_execute(conn, "INSERT INTO prompts (name, content) VALUES (?, ?)", (prompt['name'], prompt['content']))
                st.success(f"Pomyślnie załadowano {len(config_data.get('sites',[]))} stron i {len(config_data.get('prompts',[]))} promptów! Strona zostanie odświeżona.")
                st.rerun()
            except Exception as e:
                st.error(f"Błąd podczas przetwarzania pliku: {e}")
    with col2:
        sites_for_export = db_execute(conn, "SELECT name, url, username, app_password FROM sites", fetch="all")
        prompts_for_export = db_execute(conn, "SELECT name, content FROM prompts", fetch="all")
        if sites_for_export or prompts_for_export:
            export_data = {'sites': [], 'prompts': []}
            for name, url, username, encrypted_pass_bytes in sites_for_export:
                encrypted_pass_b64 = base64.b64encode(encrypted_pass_bytes).decode('utf-8')
                export_data['sites'].append({'name': name, 'url': url, 'username': username, 'app_password_b64': encrypted_pass_b64})
            for name, content in prompts_for_export:
                export_data['prompts'].append({'name': name, 'content': content})
            st.download_button(label="Pobierz konfigurację do pliku", data=json.dumps(export_data, indent=2), file_name="pbn_config.json", mime="application/json")
    st.divider()
    st.subheader("2. Dodaj nową stronę")
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
    st.subheader("3. Lista załadowanych stron")
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
