const byId = id => document.getElementById(id);
const form = byId('studio-form');
if (window.parent !== window) document.querySelector('header').hidden = true;
const photoInput = byId('photo');
const generateButton = byId('generate');
let selectedPhoto = null;
let photoUrl = null;
let serverConfigured = false;
let providers = {};
let configReady = false;
let busy = false;

function setStatus(message, kind = '') {
  byId('status').className = `status ${kind}`;
  byId('status').textContent = message;
}

function updateConnection() {
  const provider = providers[byId('provider').value];
  serverConfigured = Boolean(provider?.configured);
  const ready = serverConfigured || Boolean(byId('api-key').value.trim());
  byId('connection-text').textContent = ready ? 'Chave disponível · acesso será validado ao gerar' : 'Conecte sua chave para gerar';
  byId('connection').classList.toggle('ready', ready);
  generateButton.disabled = busy || !selectedPhoto || !configReady || window.sfCustomer?.status !== 'approved';
  if (window.sfCustomer?.status === 'pending') byId('connection-text').textContent = 'Cadastro aguardando liberação';
}

function updateProvider() {
  const provider = providers[byId('provider').value];
  const automatic = provider?.supports_scene === false;
  byId('scene').disabled = automatic;
  document.querySelectorAll('[data-scene]').forEach(button => { button.disabled = automatic; });
  byId('aspect-ratio').disabled = provider?.supports_aspect_ratio === false;
  const isImageProvider = provider?.image_capable === true;
  byId('provider-hint').textContent = automatic
    ? 'fal.ai SVD anima a foto automaticamente. Não aceita direção por texto; o formato depende da imagem. Use Google ou deAPI para dirigir a cena e escolher o formato.'
    : isImageProvider
      ? 'Gera uma imagem profissional do produto usando IA. Consulte preços e saldo no painel do provedor antes de gerar.'
      : 'A foto e a descrição serão enviadas ao provedor selecionado. Consulte preços e saldo no painel dele antes de gerar.';
  byId('format-label').textContent = automatic ? 'Formato da foto' : byId('aspect-ratio').value;
  byId('quality').disabled = true;
  updateConnection();
}
byId('provider').addEventListener('change', () => {
  byId('api-key').value = '';
  updateProvider();
});

function selectPhoto(file) {
  if (busy || !file) return;
  if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type) || file.size > 10 * 1024 * 1024) {
    setStatus('Escolha uma foto JPG, PNG ou WEBP de até 10 MB.', 'error');
    photoInput.value = '';
    return;
  }
  const nextUrl = URL.createObjectURL(file);
  const check = new Image();
  check.onload = () => {
    if (check.naturalWidth * check.naturalHeight > 25000000) {
      URL.revokeObjectURL(nextUrl);
      setStatus('A foto deve ter no máximo 25 megapixels.', 'error');
      return;
    }
    if (photoUrl) URL.revokeObjectURL(photoUrl);
    selectedPhoto = file;
    photoUrl = nextUrl;
    for (const id of ['source-image', 'stage-image']) {
      byId(id).src = photoUrl;
      byId(id).hidden = false;
    }
    for (const id of ['placeholder', 'upload-icon', 'upload-hint', 'result-video', 'download']) byId(id).hidden = true;
    byId('result-video').pause();
    byId('result-video').removeAttribute('src');
    byId('pick-photo').textContent = 'Trocar foto';
    byId('file-label').textContent = file.name;
    byId('stage-label').textContent = 'Foto de referência · ainda não é um vídeo';
    setStatus('Foto pronta. Descreva a cena e gere o vídeo.');
    updateConnection();
  };
  check.onerror = () => { URL.revokeObjectURL(nextUrl); setStatus('Não foi possível ler a foto. Escolha outra imagem.', 'error'); };
  check.src = nextUrl;
}

byId('pick-photo').addEventListener('click', () => photoInput.click());
photoInput.addEventListener('change', () => selectPhoto(photoInput.files[0]));
const dropZone = byId('upload-zone');
for (const name of ['dragover', 'dragleave', 'drop']) {
  dropZone.addEventListener(name, event => {
    event.preventDefault();
    dropZone.classList.toggle('dragging', name === 'dragover');
    if (name === 'drop') selectPhoto(event.dataTransfer.files[0]);
  });
}
byId('api-key').addEventListener('input', updateConnection);
byId('aspect-ratio').addEventListener('change', () => {
  const ratio = byId('aspect-ratio').value;
  byId('preview-frame').classList.toggle('landscape', ratio === '16:9');
  byId('format-label').textContent = ratio;
});
const scenes = {
  studio: 'Apresente o produto em um estúdio com fundo neutro, iluminação suave e movimento lento de câmera. Valorize os detalhes e mantenha o produto fiel à foto. Sem textos ou narração.',
  lifestyle: 'Apresente o produto em um ambiente cotidiano elegante e coerente com seu uso, com luz natural e movimento suave de câmera. Preserve os detalhes reais do produto. Sem textos ou narração.',
  cinematic: 'Apresente o produto com luz cinematográfica lateral, fundo escuro e movimento suave de câmera. Preserve os detalhes reais, as cores e a marca da foto. Sem textos ou narração.',
  detail: 'Crie uma aproximação lenta e cinematográfica para destacar o acabamento e os detalhes do produto. Use luz lateral suave, fundo discreto e mantenha as cores e a marca fiéis à foto. Sem textos ou narração.'
};
document.querySelectorAll('[data-scene]').forEach(button => button.addEventListener('click', () => {
  byId('scene').value = scenes[button.dataset.scene];
  byId('scene').dispatchEvent(new Event('input'));
  document.querySelectorAll('[data-scene]').forEach(item => {
    item.classList.toggle('active', item === button);
    item.setAttribute('aria-pressed', String(item === button));
  });
}));

form.addEventListener('submit', async event => {
  event.preventDefault();
  if (busy || !selectedPhoto || !form.reportValidity()) return;
  if (window.sfCustomer?.status !== 'approved') {
    setStatus('Seu cadastro aguarda liberação da administração.', 'error');
    return;
  }
  if (!serverConfigured && !byId('api-key').value.trim()) {
    byId('key-settings').open = true;
    byId('api-key').focus();
    setStatus('Conecte sua chave da IA selecionada. Cada provedor tem sua própria chave.', 'error');
    return;
  }
  const body = new FormData(form);
  if (providers[byId('provider').value]?.supports_scene === false) {
    body.set('scene', 'Animacao automatica baseada na foto, sem direcao por texto.');
  }
  body.append('file', selectedPhoto);
  body.append('api_key', byId('api-key').value.trim());
  const providerSelect = byId('provider');
  const providerName = providerSelect ? providerSelect.options[providerSelect.selectedIndex].text : 'IA';
  busy = true;
  const controls = [...form.querySelectorAll('button,input,textarea,select')];
  controls.forEach(control => { control.disabled = true; });
  generateButton.textContent = 'Gerando sua cena…';
  byId('download').hidden = true;
  byId('status').className = 'status';
  byId('status').replaceChildren(Object.assign(document.createElement('span'), {className: 'spinner'}), document.createTextNode(`O ${providerName} está gerando seu conteudo. Isso pode levar alguns minutos. Mantenha esta página aberta.`));
  try {
    const response = await fetch('/api/studio/generate', {method: 'POST', body});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Confira a foto e os campos preenchidos.');
    if (!data.download_url?.startsWith('/api/download/cads_ai_produto_')) throw new Error('O servidor retornou um endereço invalido.');
    const isImage = data.media_type === 'image' || data.filename.endsWith('.jpg') || data.filename.endsWith('.png');
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
    byId('stage-label').textContent = isImage ? 'Imagem gerada com IA' : 'Vídeo gerado com IA';
    window.sfRefreshNotifications?.().catch(() => {});
    setStatus('Seu vídeo está pronto e salvo na sua conta StealthForge. Confira os detalhes antes de publicar.', 'success');
    if (window.parent !== window) window.parent.postMessage({type: 'cads-studio-generated'}, location.origin);
  } catch (error) {
    setStatus(error instanceof TypeError ? 'A conexão foi interrompida. Confira a atividade recente e o uso no painel do provedor antes de gerar novamente.' : error.message, 'error');
  } finally {
    busy = false;
    controls.forEach(control => { control.disabled = false; });
    generateButton.textContent = '✦ Gerar com IA';
    updateProvider();
  }
});
window.addEventListener('beforeunload', event => { if (busy) { event.preventDefault(); event.returnValue = ''; } });
fetch('/api/studio/config').then(response => {
  if (!response.ok) throw new Error('config');
  return response.json();
}).then(config => {
  providers = config.providers;
  const select = byId('provider');
  select.replaceChildren();
  for (const [id, provider] of Object.entries(providers)) {
    select.add(new Option(provider.name + (provider.image_capable ? ' [Imagem]' : provider.video_capable ? ' [Video]' : ''), id));
  }
  select.value = config.default;
  configReady = true;
  updateProvider();
}).catch(() => {
  updateConnection();
  setStatus('Não foi possível consultar o servidor. Atualize a página antes de gerar.', 'error');
});


const updateSceneCount = () => { byId('scene-count').textContent = `${byId('scene').value.length}/500`; };
byId('scene').addEventListener('input', updateSceneCount);
updateSceneCount();
byId('toggle-key').addEventListener('click', () => {
  const visible = byId('api-key').type === 'password';
  byId('api-key').type = visible ? 'text' : 'password';
  byId('toggle-key').setAttribute('aria-label', visible ? 'Ocultar chave' : 'Mostrar chave');
  byId('toggle-key').setAttribute('aria-pressed', String(visible));
});
document.addEventListener('sf:account', updateConnection);
window.sfAccountReady.then(updateConnection);
window.lucide?.createIcons();
