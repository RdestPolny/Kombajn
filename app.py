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
    """Generuje klucz i zapisuje go do pliku."""
    key = Fernet.generate_key()
    with open(KEY_FILE, "wb") as key_file:
        key_file.write(key)
    return key

def load_key():
    """Wczytuje klucz z pliku lub generuje nowy, jeśli nie istnieje."""
    if not os.path.exists(KEY_FILE):
        return generate_key()
    with open(KEY_FILE, "rb") as key_file:
        return key_file.read()

KEY = load_key()
FERNET = Fernet(KEY)

def encrypt_data(data: str) -> bytes:
    """Szyfruje dane."""
    return FERNET.encrypt(data.encode())

def decrypt_data(encrypted_data: bytes) -> str:
    """Odszyfrowuje dane."""
    return FERNET.decrypt(encrypted_data).decode()

# Inicjalizacja bazy danych
def init_db():
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # Tabela stron
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL,
        app_password BLOB NOT NULL
    )
    """)
    # Tabela zaplanowanych wpisów (uproszczona)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS scheduled_posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        site_ids TEXT NOT NULL,
        title TEXT NOT NULL,
        content TEXT NOT NULL,
        categories TEXT,
        tags TEXT,
        publish_date TEXT NOT NULL,
        status TEXT DEFAULT 'pending'
    )
    """)
    conn.commit()
    conn.close()

# --- FUNKCJE DO ZARZĄDZANIA BAZĄ DANYCH ---

def db_execute(query, params=(), fetch=None):
    """Uniwersalna funkcja do interakcji z bazą danych."""
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
        """Testuje połączenie, sprawdzając, czy można pobrać dane użytkownika."""
        try:
            response = requests.get(f"{self.base_url}/users/me", auth=self.auth, timeout=10)
            response.raise_for_status()
            return True, "Połączenie udane!"
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                return False, "Błąd autoryzacji. Sprawdź login i hasło aplikacji."
            return False, f"Błąd HTTP: {e.response.status_code}"
        except requests.exceptions.RequestException as e:
            return False, f"Błąd połączenia: {e}"

    def get_stats(self):
        """Pobiera podstawowe statystyki: liczbę wpisów i datę ostatniego."""
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
        """Pobiera listę kategorii."""
        try:
            response = requests.get(f"{self.base_url}/categories", params={"per_page": 100}, auth=self.auth, timeout=10)
            response.raise_for_status()
            return {cat['name']: cat['id'] for cat in response.json()}
        except Exception:
            return {}

    def publish_post(self, title, content, status, publish_date, category_ids, tags):
        """Publikuje lub planuje wpis."""
        post_data = {
            'title': title,
            'content': content,
            'status': status,
            'date': publish_date,
            'categories': category_ids,
            'tags': tags
        }
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

# Inicjalizacja bazy przy starcie
init_db()

# Menu w panelu bocznym
menu = ["Dashboard", "Zarządzanie Stronami", "Harmonogram Publikacji"]
choice = st.sidebar.selectbox("Menu", menu)

# --- 1. WIDOK: ZARZĄDZANIE STRONAMI ---
if choice == "Zarządzanie Stronami":
    st.header("Zarządzanie Stronami")

    with st.expander("Dodaj nową stronę", expanded=True):
        with st.form("add_site_form", clear_on_submit=True):
            name = st.text_input("Przyjazna nazwa strony", placeholder="Np. Blog o ogrodnictwie")
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
                            db_execute("INSERT INTO sites (name, url, username, app_password) VALUES (?, ?, ?, ?)",
                                       (name, url, username, encrypted_password))
                            st.success(f"Strona '{name}' dodana pomyślnie!")
                        except sqlite3.IntegrityError:
                            st.error(f"Strona o URL '{url}' już istnieje w bazie.")
                    else:
                        st.error(f"Nie udało się dodać strony. Błąd: {message}")

    st.subheader("Lista podłączonych stron")
    sites = db_execute("SELECT id, name, url, username FROM sites", fetch="all")
    if not sites:
        st.info("Brak podłączonych stron. Dodaj swoją pierwszą stronę, korzystając z formularza powyżej.")
    else:
        for site_id, name, url, username in sites:
            cols = st.columns([0.4, 0.4, 0.2])
            with cols[0]:
                st.markdown(f"**{name}**")
                st.caption(url)
            with cols[1]:
                st.text(f"Login: {username}")
            with cols[2]:
                if st.button("Usuń", key=f"delete_{site_id}"):
                    db_execute("DELETE FROM sites WHERE id = ?", (site_id,))
                    st.rerun()

# --- 2. WIDOK: DASHBOARD ---
elif choice == "Dashboard":
    st.header("Dashboard")
    sites = db_execute("SELECT id, name, url, username, app_password FROM sites", fetch="all")

    if not sites:
        st.warning("Nie masz jeszcze żadnych stron. Przejdź do 'Zarządzanie Stronami', aby je dodać.")
    else:
        if st.button("Odśwież wszystkie statystyki"):
            st.cache_data.clear()

        @st.cache_data(ttl=600) # Cache na 10 minut
        def get_all_stats():
            all_data = []
            progress_bar = st.progress(0)
            status_text = st.empty()
            for i, (site_id, name, url, username, encrypted_pass) in enumerate(sites):
                status_text.text(f"Pobieranie danych dla: {name}...")
                password = decrypt_data(encrypted_pass)
                api = WordPressAPI(url, username, password)
                stats = api.get_stats()
                all_data.append({
                    "Nazwa": name,
                    "URL": url,
                    "Liczba wpisów": stats['total_posts'],
                    "Ostatni wpis": stats['last_post_date']
                })
                progress_bar.progress((i + 1) / len(sites))
            status_text.text("Gotowe!")
            progress_bar.empty()
            return all_data

        stats_data = get_all_stats()
        df = pd.DataFrame(stats_data)
        
        total_posts_sum = pd.to_numeric(df['Liczba wpisów'], errors='coerce').sum()

        col1, col2 = st.columns(2)
        col1.metric("Liczba podłączonych stron", len(sites))
        col2.metric("Łączna liczba wpisów", f"{int(total_posts_sum):,}".replace(",", " "))
        
        st.dataframe(df, use_container_width=True)

# --- 3. WIDOK: HARMONOGRAM PUBLIKACJI ---
elif choice == "Harmonogram Publikacji":
    st.header("Harmonogram Publikacji")
    sites = db_execute("SELECT id, name FROM sites", fetch="all")
    site_options = {name: site_id for site_id, name in sites}

    if not site_options:
        st.warning("Musisz najpierw dodać strony w panelu 'Zarządzanie Stronami', aby móc planować wpisy.")
    else:
        with st.form("schedule_post_form"):
            st.subheader("Nowy wpis")
            selected_sites_names = st.multiselect("Wybierz strony docelowe", options=site_options.keys())
            
            title = st.text_input("Tytuł wpisu")
            content = st.text_area("Treść wpisu (obsługuje HTML)", height=300)
            
            cols_meta = st.columns(2)
            with cols_meta[0]:
                categories_str = st.text_input("Kategorie (oddzielone przecinkami)", placeholder="News, Poradniki")
            with cols_meta[1]:
                tags_str = st.text_input("Tagi (oddzielone przecinkami)", placeholder="seo, wordpress")

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

                            # Pobierz ID kategorii
                            available_categories = api.get_categories()
                            target_category_ids = []
                            if categories_str:
                                input_categories = [cat.strip() for cat in categories_str.split(',')]
                                for cat_name in input_categories:
                                    if cat_name in available_categories:
                                        target_category_ids.append(available_categories[cat_name])
                                    else:
                                        st.warning(f"Na stronie '{site_name}' nie znaleziono kategorii '{cat_name}'. Zostanie ona pominięta.")

                            # Pobierz ID tagów (uproszczone - wymagałoby tworzenia tagów, jeśli nie istnieją)
                            # W tej wersji przekazujemy stringi, WP powinien je obsłużyć
                            target_tags = [tag.strip() for tag in tags_str.split(',')] if tags_str else []

                            success, message = api.publish_post(
                                title=title,
                                content=content,
                                status="future",
                                publish_date=publish_datetime,
                                category_ids=target_category_ids,
                                tags=target_tags
                            )
                            if success:
                                st.success(f"[{site_name}]: {message}")
                            else:
                                st.error(f"[{site_name}]: {message}")
