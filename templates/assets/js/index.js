// Texto digitado no topo
const texto = "SEU SISTEMA ESTÁ ENTRANDO";
const textoElemento = document.querySelector(".texto");

let i = 0;
function digitar() {
  if (i < texto.length) {
    textoElemento.textContent += texto.charAt(i);
    i++;
    setTimeout(digitar, 100);
  }
}
digitar();

// Logs automáticos
const logs = [
  "[14:00] MANIFESST 543534 ENTRANDO",
  "[14:01] MANIFESST 321231 ENTRANDO",
  "[14:02] MANIFESST 352345 ENTRANDO",
  "[14:03] MANIFESST 653634 ENTRANDO",
  "[14:04] MANIFESST 423423 ENTRANDO",
  "[14:05] MANIFESST 342371 ENTRANDO",
  "[14:06] MANIFESST 878978 ENTRANDO",
  "[14:07] MANIFESST 123212 ENTRANDO",
  "[14:08] MANIFESST 424334 ENTRANDO"
];

const logsContainer = document.getElementById("logs");

let j = 0;
function mostrarLog() {
  if (j < logs.length) {
    const p = document.createElement("p");
    p.textContent = logs[j];
    logsContainer.appendChild(p);
    logsContainer.scrollTop = logsContainer.scrollHeight; // scroll automático
    j++;
    setTimeout(mostrarLog, 700);
  }
}
setTimeout(mostrarLog, 2500); // espera terminar o typing antes de começar
