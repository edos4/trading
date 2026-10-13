/* Documentation tab: smooth TOC jumps and an active-section highlight. */
(function () {
  const links = Array.from(document.querySelectorAll(".toc-link"));
  const content = document.getElementById("docs-content");
  if (!links.length || !content) return;

  const byId = new Map();
  for (const link of links) {
    const id = decodeURIComponent((link.getAttribute("href") || "").slice(1));
    if (id) byId.set(id, link);
  }

  function setActive(link) {
    for (const l of links) l.classList.toggle("active", l === link);
    if (link) link.scrollIntoView({ block: "nearest" });
  }

  for (const link of links) {
    link.addEventListener("click", (ev) => {
      const id = decodeURIComponent((link.getAttribute("href") || "").slice(1));
      const target = document.getElementById(id);
      if (!target) return;
      ev.preventDefault();
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      history.replaceState(null, "", `#${id}`);
      setActive(link);
    });
  }

  const observer = new IntersectionObserver(
    (entries) => {
      for (const entry of entries) {
        if (!entry.isIntersecting) continue;
        const link = byId.get(entry.target.id);
        if (link) setActive(link);
      }
    },
    { rootMargin: "-15% 0px -75% 0px", threshold: 0 },
  );
  for (const heading of content.querySelectorAll("h1[id], h2[id], h3[id], h4[id], h5[id], h6[id]")) {
    observer.observe(heading);
  }

  if (location.hash) {
    const link = byId.get(decodeURIComponent(location.hash.slice(1)));
    if (link) setActive(link);
  }
})();
