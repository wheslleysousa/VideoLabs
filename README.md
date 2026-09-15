# VideoLabs

VideoLabs gera automaticamente variações de vídeos combinando blocos como Hook, Corpo, Desenvolvimento e CTA.

## V0

A primeira versão valida o motor usando Google Colab + Google Drive + FFmpeg. O celular serve apenas para controlar o processo; a renderização acontece no Colab.

### Estrutura esperada no Drive

```text
VideoLabs/
  input/
    Projeto01/
      01-hook/
      02-corpo/
      03-desenvolvimento/
      04-cta/
  output/
    Projeto01/
```

As etapas são dinâmicas. Você pode usar 2, 3, 4 ou mais pastas, desde que estejam numeradas na ordem em que devem aparecer no vídeo.

## Objetivo

1. Detectar automaticamente as etapas e vídeos.
2. Calcular todas as combinações possíveis.
3. Concatenar os clipes com FFmpeg.
4. Salvar os resultados diretamente no Google Drive.
5. Manter um manifesto para retomar trabalhos interrompidos.

## Próximas versões

- Validação automática com ffprobe.
- Normalização de vídeos incompatíveis.
- Interface mobile.
- Processamento automatizado com GitHub Actions.
