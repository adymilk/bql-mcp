const repository = "adymilk/bql-mcp";
const fallbackCommand = "curl -fsSL https://github.com/adymilk/bql-mcp/raw/refs/heads/main/install.sh | bash";

async function resolveLatestInstall() {
  const installCommand = document.getElementById("install-command");
  const agentPrompt = document.getElementById("agent-install-prompt");
  if (!installCommand || !agentPrompt) return;

  try {
    const response = await fetch(
      `https://api.github.com/repos/${repository}/commits/main`,
      {
        headers: { Accept: "application/vnd.github+json" },
        cache: "no-store",
      },
    );
    if (!response.ok) throw new Error(`GitHub API ${response.status}`);

    const { sha } = await response.json();
    if (!/^[0-9a-f]{40}$/i.test(sha)) throw new Error("Invalid commit SHA");

    const command = `curl -fsSL https://raw.githubusercontent.com/${repository}/${sha}/install.sh | BQL_REF=${sha} bash`;
    installCommand.textContent = command;
    agentPrompt.textContent = "帮我安装最新版 BQL Minitab MCP，并完成客户端注册和自检。";
    agentPrompt.dataset.copyValue = `帮我安装 BQL MCP：执行 ${command}，然后注册 bql、运行 bql-mcp --self-check，并告诉我版本和配置状态。`;

    const versionResponse = await fetch(
      `https://raw.githubusercontent.com/${repository}/${sha}/VERSION`,
      { cache: "no-store" },
    );
    if (versionResponse.ok) {
      const version = (await versionResponse.text()).trim();
      if (/^\d+\.\d+\.\d+$/.test(version)) {
        for (const element of document.querySelectorAll("[data-version]")) {
          element.textContent = `v${version}`;
        }
      }
    }
  } catch {
    installCommand.textContent = fallbackCommand;
    agentPrompt.textContent = "帮我安装最新版 BQL Minitab MCP，并完成客户端注册和自检。";
    agentPrompt.dataset.copyValue = `帮我安装 BQL MCP：执行 ${fallbackCommand}，然后注册 bql、运行 bql-mcp --self-check，并告诉我版本和配置状态。`;
  }
}

resolveLatestInstall();

const copyButtons = document.querySelectorAll("[data-copy-target]");

for (const button of copyButtons) {
  button.addEventListener("click", async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;

    const text = target.dataset.copyValue || target.textContent.trim();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const area = document.createElement("textarea");
      area.value = text;
      area.setAttribute("readonly", "");
      area.style.position = "fixed";
      area.style.opacity = "0";
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }

    button.classList.add("copied");
    button.setAttribute("aria-label", "已复制安装命令");
    window.setTimeout(() => {
      button.classList.remove("copied");
      button.setAttribute("aria-label", "复制安装命令");
    }, 1800);
  });
}

const year = document.getElementById("year");
if (year) {
  year.textContent = new Date().getFullYear();
}
