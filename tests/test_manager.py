from kombajn import PBNManager

def test_manager_persists_sites(tmp_path):
    storage = tmp_path / "sites.json"
    m1 = PBNManager(str(storage))
    m1.add_site("http://example.com", "user", "pass")
    assert storage.exists()

    # Reload manager from disk
    m2 = PBNManager(str(storage))
    assert len(m2.clients) == 1
    site = m2.clients[0].site
    assert site.url == "http://example.com"
    assert site.username == "user"
    assert site.password == "pass"
