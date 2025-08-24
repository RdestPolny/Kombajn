import streamlit as st
import sqlite3
import pandas as pd
import requests
from requests.auth import HTTPBasicAuth
from datetime import datetime
import json
import os
from cryptography.fernet import Fernet

# --- KONFIGURACJA I INICJALIZACJA ---

# Stałe
DB_FILE = "pbn_data.db"
KEY_FILE = "secret.key"

# Funkcje do szyfrowania danych dostępowych
def generate_key():
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as key_file:
        key_file.write(key)
    return key

def load_key():
    if not os.path.exists(KEY_FILE):
        return generate_key()
    with open(KEY_FILE, "rb") as key_file:
        return key_file.read()

KEY = load_key()
FERNET = Fernet(KEY)

def encrypt_data(data: str) -> bytes:
    return FERNET.encrypt(data.encode())

def decrypt_data(encrypted_data: bytes) -> str:
    return FERNET.decrypt(encrypted_data).decode()

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL,
        app_password BLOB NOT NULL
    )
    """)
    conn.commit()
    conn.close()

def db_execute(query, params=(), fetch=None):
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(query, params)
    if fetch == "one":
        result = cursor.fetchone()
    elif fetch == "all":
        result = cursor.fetchall()
    else:
        result = None
    conn.commit()
    conn.close()
    return result

# --- KLASA DO OBSŁUGI WORDPRESS REST API ---

class WordPressAPI:
    def __init__(self, url, username, password):
        self.base_url = url.rstrip('/') + "/wp-json/wp/v2"
        self.auth = HTTPBasicAuth(username, password)

    def test_connection(self):
        try:
            response = requests.get(f"{self.base_url}/users/me", auth=self.auth, timeout=10)
            response.raise_for_status()
            return True, "Połączenie udane!"
        except requests.exceptions.HTTPError as e:
            return False, f"Błąd HTTP ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e:
            return False, f"Błąd połączenia: {e}"

    def get_stats(self):
        try:
            response = requests.get(f"{self.base_url}/posts", params={"per_page": 1, "orderby": "date"}, auth=self.auth, timeout=10)
            response.raise_for_status()
            total_posts = int(response.headers.get('X-WP-Total', 0))
            last_post_date = "Brak wpisów"
            if total_posts > 0 and response.json():
                last_post_date = datetime.fromisoformat(response.json()[0]['date']).strftime('%Y-%m-%d %H:%M')
            return {"total_posts": total_posts, "last_post_date": last_post_date}
        except Exception:
            return {"total_posts": "Błąd", "last_post_date": "Błąd"}

    def get_categories(self):
        try:
            response = requests.get(f"{self.base_url}/categories", params={"per_page": 100}, auth=self.auth, timeout=10)
            response.raise_for_status()
            return {cat['name']: cat['id'] for cat in response.json()}
        except Exception:
            return {}

    def get_users(self):
        try:
            response = requests.get(f"{self.base_url}/users", params={"per_page": 100, "roles": "administrator,editor,author"}, auth=self.auth, timeout=10)
            response.raise_for_status()
            return {user['name']: user['id'] for user in response.json()}
        except Exception:
            return {}

    def get_posts(self, per_page=25):
        try:
            response = requests.get(f"{self.base_url}/posts", params={"per_page": per_page, "orderby": "date", "_embed": "author,wp:term"}, auth=self.auth, timeout=15)
            response.raise_for_status()
            posts = []
            for item in response.json():
                author_name = item['_embedded']['author'][0]['name'] if 'author' in item['_embedded'] else 'N/A'
                categories = [term['name'] for term in item['_embedded']['wp:term'][0]] if 'wp:term' in item['_embedded'] else []
                posts.append({
                    "id": item['id'],
                    "title": item['title']['rendered'],
                    "date": datetime.fromisoformat(item['date']).strftime('%Y-%m-%d %H:%M'),
                    "author": author_name,
                    "categories": ", ".join(categories)
                })
            return posts
        except Exception as e:
            st.error(f"Błąd podczas pobierania wpisów: {e}")
            return []

    def update_post(self, post_id, data):
        try:
            response = requests.post(f"{self.base_url}/posts/{post_id}", json=data, auth=self.auth, timeout=15)
            response.raise_for_status()
            return True, f"Wpis ID {post_id} zaktualizowany."
        except requests.exceptions.HTTPError as e:
            return False, f"Błąd aktualizacji wpisu ID {post_id} ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e:
            return False, f"Błąd sieci przy aktualizacji wpisu ID {post_id}: {e}"

    def publish_post(self, title, content, status, publish_date, category_ids, tags):
        post_data = {'title': title, 'content': content, 'status': status, 'date': publish_date, 'categories': category_ids, 'tags': tags}
        try:
            response = requests.post(f"{self.base_url}/posts", json=post_data, auth=self.auth, timeout=15)
            response.raise_for_status()
            return True, f"Wpis opublikowany/zaplanowany pomyślnie! ID: {response.json()['id']}"
        except requests.exceptions.HTTPError as e:
            return False, f"Błąd publikacji ({e.response.status_code}): {e.response.text}"
        except requests.exceptions.RequestException as e:
            return False, f"Błąd sieci podczas publikacji: {e}"

# --- INTERFEJS UŻYTKOWNIKA (STREAMLIT) ---

st.set_page_config(layout="wide", page_title="PBN Manager")
st.title("🚀 PBN Manager")
st.caption("Centralne zarządzanie Twoją siecią blogów WordPress.")

init_db()

menu = ["Dashboard", "Zarządzanie Stronami", "Harmonogram Publikacji", "Zarządzanie Treścią"]
choice = st.sidebar.selectbox("Menu", menu)

# ... (kod dla "Dashboard", "Zarządzanie Stronami", "Harmonogram Publikacji" pozostaje bez zmian) ...
if choice == "Dashboard":
    st.header("Dashboard")
    sites = db_execute("SELECT id, name, url, username, app_password FROM sites", fetch="all")
    if not sites:
        st.warning("Nie masz jeszcze żadnych stron. Przejdź do 'Zarządzanie Stronami', aby je dodać.")
    else:
        if st.button("Odśwież wszystkie statystyki"):
            st.cache_data.clear()

        @st.cache_data(ttl=600)
        def get_all_stats():
            all_data = []
            progress_bar = st.progress(0, text="Pobieranie danych...")
            for i, (site_id, name, url, username, encrypted_pass) in enumerate(sites):
                password = decrypt_data(encrypted_pass)
                api = WordPressAPI(url, username, password)
                stats = api.get_stats()
                all_data.append({
                    "Nazwa": name, "URL": url, "Liczba wpisów": stats['total_posts'], "Ostatni wpis": stats['last_post_date']
                })
                progress_bar.progress((i + 1) / len(sites), text=f"Pobieranie danych dla: {name}")
            progress_bar.empty()
            return all_data

        stats_data = get_all_stats()
        df = pd.DataFrame(stats_data)
        total_posts_sum = pd.to_numeric(df['Liczba wpisów'], errors='coerce').sum()
        col1, col2 = st.columns(2)
        col1.metric("Liczba podłączonych stron", len(sites))
        col2.metric("Łączna liczba wpisów", f"{int(total_posts_sum):,}".replace(",", " "))
        st.dataframe(df, use_container_width=True)

elif choice == "Zarządzanie Stronami":
    st.header("Zarządzanie Stronami")
    with st.expander("Dodaj nową stronę", expanded=True):
        with st.form("add_site_form", clear_on_submit=True):
            name = st.text_input("Przyjazna nazwa strony")
            url = st.text_input("URL strony", placeholder="https://twojastrona.pl")
            username = st.text_input("Login WordPress")
            app_password = st.text_input("Hasło Aplikacji", type="password")
            submitted = st.form_submit_button("Testuj połączenie i Zapisz")
            if submitted:
                if not all([name, url, username, app_password]):
                    st.error("Wszystkie pola są wymagane!")
                else:
                    with st.spinner("Testowanie połączenia..."):
                        api = WordPressAPI(url, username, app_password)
                        success, message = api.test_connection()
                    if success:
                        encrypted_password = encrypt_data(app_password)
                        try:
                            db_execute("INSERT INTO sites (name, url, username, app_password) VALUES (?, ?, ?, ?)", (name, url, username, encrypted_password))
                            st.success(f"Strona '{name}' dodana pomyślnie!")
                        except sqlite3.IntegrityError:
                            st.error(f"Strona o URL '{url}' już istnieje w bazie.")
                    else:
                        st.error(f"Nie udało się dodać strony. Błąd: {message}")
    st.subheader("Lista podłączonych stron")
    sites = db_execute("SELECT id, name, url, username FROM sites", fetch="all")
    if not sites:
        st.info("Brak podłączonych stron.")
    else:
        for site_id, name, url, username in sites:
            cols = st.columns([0.4, 0.4, 0.2])
            cols[0].markdown(f"**{name}**\n\n{url}")
            cols[1].text(f"Login: {username}")
            if cols[2].button("Usuń", key=f"delete_{site_id}"):
                db_execute("DELETE FROM sites WHERE id = ?", (site_id,))
                st.rerun()

elif choice == "Harmonogram Publikacji":
    st.header("Harmonogram Publikacji")
    sites = db_execute("SELECT id, name FROM sites", fetch="all")
    site_options = {name: site_id for site_id, name in sites}
    if not site_options:
        st.warning("Musisz najpierw dodać strony w panelu 'Zarządzanie Stronami'.")
    else:
        with st.form("schedule_post_form"):
            st.subheader("Nowy wpis")
            selected_sites_names = st.multiselect("Wybierz strony docelowe", options=site_options.keys())
            title = st.text_input("Tytuł wpisu")
            content = st.text_area("Treść wpisu (obsługuje HTML)", height=300)
            cols_meta = st.columns(2)
            categories_str = cols_meta[0].text_input("Kategorie (oddzielone przecinkami)")
            tags_str = cols_meta[1].text_input("Tagi (oddzielone przecinkami)")
            cols_date = st.columns(2)
            publish_date = cols_date[0].date_input("Data publikacji", min_value=datetime.now())
            publish_time = cols_date[1].time_input("Godzina publikacji")
            submit_button = st.form_submit_button("Zaplanuj wpis")
            if submit_button:
                if not all([selected_sites_names, title, content]):
                    st.error("Musisz wybrać przynajmniej jedną stronę oraz podać tytuł i treść.")
                else:
                    publish_datetime = datetime.combine(publish_date, publish_time).isoformat()
                    with st.spinner("Przetwarzanie..."):
                        for site_name in selected_sites_names:
                            site_id = site_options[site_name]
                            site_info = db_execute("SELECT url, username, app_password FROM sites WHERE id = ?", (site_id,), fetch="one")
                            url, username, encrypted_pass = site_info
                            password = decrypt_data(encrypted_pass)
                            api = WordPressAPI(url, username, password)
                            available_categories = api.get_categories()
                            target_category_ids = []
                            if categories_str:
                                input_categories = [cat.strip() for cat in categories_str.split(',')]
                                for cat_name in input_categories:
                                    if cat_name in available_categories:
                                        target_category_ids.append(available_categories[cat_name])
                                    else:
                                        st.warning(f"Na stronie '{site_name}' nie znaleziono kategorii '{cat_name}'.")
                            target_tags = [tag.strip() for tag in tags_str.split(',')] if tags_str else []
                            success, message = api.publish_post(title, content, "future", publish_datetime, target_category_ids, target_tags)
                            if success: st.success(f"[{site_name}]: {message}")
                            else: st.error(f"[{site_name}]: {message}")

# --- NOWY WIDOK: ZARZĄDZANIE TREŚCIĄ ---
elif choice == "Zarządzanie Treścią":
    st.header("Zarządzanie Treścią i Masowa Edycja")

    sites = db_execute("SELECT id, name, url, username, app_password FROM sites", fetch="all")
    site_options = {site[1]: site for site in sites}

    if not site_options:
        st.warning("Dodaj przynajmniej jedną stronę w 'Zarządzanie Stronami', aby korzystać z tego modułu.")
    else:
        selected_site_name = st.selectbox("Wybierz stronę do edycji", options=site_options.keys())
        
        if selected_site_name:
            site_id, name, url, username, encrypted_pass = site_options[selected_site_name]
            password = decrypt_data(encrypted_pass)
            api = WordPressAPI(url, username, password)

            st.subheader(f"Wpisy na stronie: {name}")
            
            # Pobieranie danych (z cache)
            @st.cache_data(ttl=300)
            def get_site_data(_url, _username, _password):
                api_instance = WordPressAPI(_url, _username, _password)
                posts = api_instance.get_posts(per_page=50)
                categories = api_instance.get_categories()
                users = api_instance.get_users()
                return posts, categories, users

            posts, categories, users = get_site_data(url, username, password)

            if not posts:
                st.info("Nie znaleziono wpisów na tej stronie lub wystąpił błąd połączenia.")
            else:
                df = pd.DataFrame(posts)
                df['Zaznacz'] = False
                
                st.info("Zaznacz wpisy, które chcesz edytować, a następnie użyj formularza masowej edycji poniżej.")
                
                # Tabela z checkboxami
                edited_df = st.data_editor(
                    df,
                    column_config={"Zaznacz": st.column_config.CheckboxColumn(required=True)},
                    disabled=["id", "title", "date", "author", "categories"],
                    hide_index=True,
                    use_container_width=True
                )
                
                selected_posts = edited_df[edited_df.Zaznacz]
                
                if not selected_posts.empty:
                    st.subheader(f"Masowa edycja dla {len(selected_posts)} zaznaczonych wpisów")
                    
                    with st.form("bulk_edit_form"):
                        # Opcje edycji
                        new_category_names = st.multiselect("Zastąp kategorie", options=categories.keys())
                        new_author_name = st.selectbox("Zmień autora", options=[None] + list(users.keys()))
                        
                        submitted = st.form_submit_button("Wykonaj masową edycję")
                        
                        if submitted:
                            if not new_category_names and not new_author_name:
                                st.error("Wybierz przynajmniej jedną akcję do wykonania (nowe kategorie lub nowego autora).")
                            else:
                                update_data = {}
                                if new_category_names:
                                    update_data['categories'] = [categories[name] for name in new_category_names]
                                if new_author_name:
                                    update_data['author'] = users[new_author_name]

                                with st.spinner("Aktualizowanie wpisów..."):
                                    progress_bar = st.progress(0)
                                    total_selected = len(selected_posts)
                                    for i, post_id in enumerate(selected_posts['id']):
                                        success, message = api.update_post(post_id, update_data)
                                        if success:
                                            st.success(message)
                                        else:
                                            st.error(message)
                                        progress_bar.progress((i + 1) / total_selected)
                                
                                st.info("Proces zakończony. Odśwież dane, aby zobaczyć zmiany.")
                                st.cache_data.clear() # Czyści cache, aby wymusić ponowne załadowanie danych
                else:
                    st.caption("Zaznacz przynajmniej jeden wpis, aby aktywować panel masowej edycji.")
