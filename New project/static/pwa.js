(function () {
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/service-worker.js").catch(function (error) {
        console.error("SW register error:", error);
      });
    });
  }

  var isIOS = /iphone|ipad|ipod/i.test(window.navigator.userAgent);
  var isStandalone = window.matchMedia("(display-mode: standalone)").matches || window.navigator.standalone;
  var tip = document.getElementById("install-tip");

  if (tip && isIOS && !isStandalone) {
    tip.classList.remove("hidden");
  }
})();
