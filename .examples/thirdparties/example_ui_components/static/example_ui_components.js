// Tiny demo-only interactivity for the service-pill gallery on this page.
// A real integration attaching to Notifications gets this exact swap
// behaviour for free from notifications.html's own showService() -- this
// is just a local stand-in so the pills on *this* page do something.
function uicSwitchPill(id, el) {
  document.querySelectorAll("#uicPills .service-pill").forEach(function (btn) {
    btn.classList.remove("active");
  });
  el.classList.add("active");
  document.getElementById("uicPillResult").textContent = "Selected: " + id;
}
