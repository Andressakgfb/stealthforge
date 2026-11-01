import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

target_file = "public/studio.js"
if not os.path.exists(target_file):
    print("ERRO: " + target_file + " nao encontrado.")
    sys.exit(1)

with open(target_file, "r", encoding="utf-8") as f:
    content = f.read()

original = content

# 1. Mudar filtro do dropdown: mostrar TODOS os provedores (imagem + video)
old_filter = "if (provider.video_capable) select.add(new Option(provider.name, id));"
new_filter = "select.add(new Option(provider.name + (provider.image_capable ? ' [Imagem]' : provider.video_capable ? ' [Video]' : ''), id));"

if "provider.image_capable" in content:
    print("[OK] Filtro do dropdown ja atualizado.")
elif old_filter in content:
    content = content.replace(old_filter, new_filter)
    print("[OK] Filtro do dropdown atualizado para mostrar imagem + video.")
else:
    print("[AVISO] Filtro do dropdown nao encontrado exatamente.")

# 2. Adaptar texto de "gerando o video" para generico
old_status_text = "`O ${providerName} está gerando o vídeo. Isso pode levar alguns minutos. Mantenha esta página aberta.`"
new_status_text = "`O ${providerName} está gerando seu conteudo. Isso pode levar alguns minutos. Mantenha esta página aberta.`"

if "gerando seu conteudo" in content:
    print("[OK] Texto de status ja atualizado.")
elif old_status_text in content:
    content = content.replace(old_status_text, new_status_text)
    print("[OK] Texto de status atualizado para generico.")
else:
    print("[AVISO] Texto de status nao encontrado exatamente.")

# 3. Adaptar validacao de download_url para aceitar imagem (.jpg) tambem
old_validation = "if (!data.download_url?.startsWith('/api/download/cads_ai_produto_')) throw new Error('O servidor retornou um endereço de vídeo inválido.');"
new_validation = "if (!data.download_url?.startsWith('/api/download/cads_ai_produto_')) throw new Error('O servidor retornou um endereço invalido.');"

if "endereco invalido" in content and "video" not in content.split("endereco invalido")[0][-50:]:
    print("[OK] Validacao de URL ja atualizada.")
elif old_validation in content:
    content = content.replace(old_validation, new_validation)
    print("[OK] Validacao de URL atualizada para generico.")
else:
    print("[AVISO] Validacao de URL nao encontrada exatamente.")

# 4. Adaptar exibicao de resultado: se for imagem, usar <img> em vez de <video>
# Hoje o codigo assume result-video sempre. Vamos adicionar logica condicional.
old_result_display = """byId('result-video').src = data.download_url;
    byId('result-video').hidden = false;
    byId('stage-image').hidden = true;
    byId('placeholder').hidden = true;
    byId('download').href = data.download_url;
    byId('download').download = data.filename;
    byId('download').hidden = false;
    byId('stage-label').textContent = 'Vídeo gerado com IA';"""

new_result_display = """const isImage = data.media_type === 'image' || data.filename.endsWith('.jpg') || data.filename.endsWith('.png');
    if (isImage) {
      byId('result-video').hidden = true;
      byId('result-video').pause();
      byId('result-video').removeAttribute('src');
      let resultImg = document.getElementById('result-image');
      if (!resultImg) {
        resultImg = document.createElement('img');
        resultImg.id = 'result-image';
        resultImg.className = 'result-media';
        resultImg.style.maxWidth = '100%';
        resultImg.style.borderRadius = '8px';
        byId('result-video').parentNode.insertBefore(resultImg, byId('result-video'));
      }
      resultImg.src = data.download_url;
      resultImg.hidden = false;
    } else {
      const resultImg = document.getElementById('result-image');
      if (resultImg) resultImg.hidden = true;
      byId('result-video').src = data.download_url;
      byId('result-video').hidden = false;
    }
    byId('stage-image').hidden = true;
    byId('placeholder').hidden = true;
    byId('download').href = data.download_url;
    byId('download').download = data.filename;
    byId('download').hidden = false;
    byId('stage-label').textContent = isImage ? 'Imagem gerada com IA' : 'Vídeo gerado com IA';"""

if "data.media_type" in content:
    print("[OK] Exibicao de resultado ja adaptada.")
elif old_result_display in content:
    content = content.replace(old_result_display, new_result_display)
    print("[OK] Exibicao de resultado adaptada para imagem + video.")
else:
    print("[AVISO] Bloco de exibicao de resultado nao encontrado exatamente. Tentando fallback...")
    # Fallback: procurar por partes
    if "byId('result-video').src = data.download_url;" in content:
        content = content.replace(
            "byId('result-video').src = data.download_url;",
            "const isImage = data.media_type === 'image' || data.filename.endsWith('.jpg');\n    if (!isImage) { byId('result-video').src = data.download_url; byId('result-video').hidden = false; } else { byId('result-video').hidden = true; let ri = document.getElementById('result-image'); if(!ri){ri=document.createElement('img');ri.id='result-image';ri.style.maxWidth='100%';byId('result-video').parentNode.insertBefore(ri,byId('result-video'));} ri.src=data.download_url; ri.hidden=false; }"
        )
        content = content.replace(
            "byId('stage-label').textContent = 'Vídeo gerado com IA';",
            "byId('stage-label').textContent = isImage ? 'Imagem gerada com IA' : 'Vídeo gerado com IA';"
        )
        print("[OK] Exibicao de resultado adaptada via fallback.")

# 5. Adaptar texto do botao de gerar
old_button_text = "generateButton.textContent = '✦ Gerar vídeo com IA';"
new_button_text = "generateButton.textContent = '✦ Gerar com IA';"

if "Gerar com IA'" in content and "video" not in content.split("Gerar com IA'")[0][-30:]:
    print("[OK] Texto do botao ja atualizado.")
elif old_button_text in content:
    content = content.replace(old_button_text, new_button_text)
    print("[OK] Texto do botao atualizado para generico.")
else:
    print("[AVISO] Texto do botao nao encontrado exatamente.")

# 6. Adaptar updateProvider para provedores de imagem
old_provider_hint = """byId('provider-hint').textContent = automatic
    ? 'fal.ai SVD anima a foto automaticamente. Não aceita direção por texto; o formato depende da imagem. Use Google ou deAPI para dirigir a cena e escolher o formato.'
    : 'A foto e a descrição serão enviadas ao provedor selecionado. Consulte preços e saldo no painel dele antes de gerar.';"""

new_provider_hint = """const isImageProvider = provider?.image_capable === true;
  byId('provider-hint').textContent = automatic
    ? 'fal.ai SVD anima a foto automaticamente. Não aceita direção por texto; o formato depende da imagem. Use Google ou deAPI para dirigir a cena e escolher o formato.'
    : isImageProvider
      ? 'Gera uma imagem profissional do produto usando IA. Consulte preços e saldo no painel do provedor antes de gerar.'
      : 'A foto e a descrição serão enviadas ao provedor selecionado. Consulte preços e saldo no painel dele antes de gerar.';"""

if "isImageProvider" in content:
    print("[OK] Hint do provedor ja atualizado.")
elif old_provider_hint in content:
    content = content.replace(old_provider_hint, new_provider_hint)
    print("[OK] Hint do provedor atualizado para imagem.")
else:
    print("[AVISO] Hint do provedor nao encontrado exatamente.")

if content == original:
    print("[NADA] Nenhuma alteracao aplicada. Verifique os marcadores.")
else:
    with open(target_file, "w", encoding="utf-8") as f:
        f.write(content)
    print("[FIM] Frontend patcheado com sucesso!")

print("[VALIDACAO] Verificando integridade do arquivo...")
with open(target_file, "r", encoding="utf-8") as f:
    final = f.read()
checks = [
    ("provider.image_capable", "Filtro dropdown"),
    ("gerando seu conteudo", "Texto status"),
    ("data.media_type", "Exibicao resultado"),
    ("Gerar com IA'", "Texto botao"),
]
for pattern, label in checks:
    if pattern in final:
        print(f"  [OK] {label}")
    else:
        print(f"  [AVISO] {label} - padrao nao encontrado")