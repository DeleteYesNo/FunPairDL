const API_URL = "http://127.0.0.1:9172/api";

async function checkStatus() {
  const dot = document.getElementById("statusDot");
  const text = document.getElementById("statusText");
  const queueInfo = document.getElementById("queueInfo");
  const queueCount = document.getElementById("queueCount");

  try {
    const resp = await fetch(`${API_URL}/status`);
    if (resp.ok) {
      const data = await resp.json();
      dot.classList.add("online");
      text.textContent = `Online (v${data.version})`;

      // Get queue info
      const qResp = await fetch(`${API_URL}/queue`);
      if (qResp.ok) {
        const qData = await qResp.json();
        queueInfo.style.display = "block";
        queueCount.textContent = qData.total_pairs;
      }
    } else {
      dot.classList.remove("online");
      text.textContent = "Server error";
    }
  } catch (e) {
    dot.classList.remove("online");
    text.textContent = "Offline - Start FunPairDL";
  }
}

checkStatus();
