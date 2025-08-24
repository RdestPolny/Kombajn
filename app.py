"""Streamlit application providing a minimal PBN management panel.

The interface allows registering multiple WordPress sites, viewing
aggregated statistics and scheduling future posts.  It is a simple
starting point for a more feature rich dashboard.
"""
from __future__ import annotations

import datetime as dt

import streamlit as st

from pbn_manager import PBNManager


# ---------------------------------------------------------------------------
# Initialise manager in the session state so added sites persist across
# Streamlit script reruns.
if "manager" not in st.session_state:
    st.session_state.manager = PBNManager()

manager: PBNManager = st.session_state.manager


# ---------------------------------------------------------------------------
# Sidebar form for registering new WordPress sites.
st.sidebar.header("Add WordPress Site")
with st.sidebar.form("add-site"):
    url = st.text_input("Site URL", placeholder="https://example.com")
    username = st.text_input("Username")
    password = st.text_input("Password", type="password")
    submitted = st.form_submit_button("Add")
    if submitted:
        if url and username and password:
            manager.add_site(url, username, password)
            st.sidebar.success(f"Added {url}")
        else:
            st.sidebar.error("All fields required")


# ---------------------------------------------------------------------------
st.title("PBN Manager")

st.header("Aggregated statistics")
if manager.clients:
    stats = manager.aggregate_stats()
    st.json(stats)
else:
    st.info("No sites registered yet")


st.header("Schedule a post")
if manager.clients:
    with st.form("schedule-post"):
        site_url = st.selectbox("Site", [c.site.url for c in manager.clients])
        title = st.text_input("Title")
        content = st.text_area("Content")
        categories = st.text_input("Category IDs (comma separated)")
        publish_date = st.date_input("Publish date", dt.date.today())
        publish_time = st.time_input("Publish time", dt.time(10, 0))
        submit = st.form_submit_button("Schedule")
        if submit:
            publish_dt = dt.datetime.combine(publish_date, publish_time)
            category_ids = [int(x.strip()) for x in categories.split(",") if x.strip()]
            for client in manager.clients:
                if client.site.url == site_url:

                 
else:
    st.info("Register a site to schedule posts")

