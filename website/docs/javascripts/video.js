// Click-to-load YouTube players for the Videos page.
//
// Each `.video[data-youtube-id]` block starts as a plain link to YouTube, which
// is what visitors without JavaScript get. With JavaScript, the link becomes a
// play button; the YouTube iframe (privacy-enhanced domain) is created only
// after a click, so nothing loads from YouTube on page view.

function upgradeVideo(block) {
  const videoId = block.dataset.youtubeId;
  const link = block.querySelector("a");
  const length = block.querySelector(".video__length");
  if (!videoId || !link) {
    return;
  }

  const title = link.textContent.trim();
  const button = document.createElement("button");
  button.type = "button";
  button.className = "video__play";
  button.setAttribute("aria-label", `Play ${title}`);
  button.innerHTML =
    '<span class="video__glyph" aria-hidden="true">&#9654;</span>' +
    '<span class="video__title"></span>' +
    '<span class="video__meta"></span>';
  button.querySelector(".video__title").textContent = title;
  button.querySelector(".video__meta").textContent = length
    ? `${length.textContent.trim()}, plays from YouTube`
    : "plays from YouTube";

  button.addEventListener("click", () => {
    const player = document.createElement("iframe");
    player.src = `https://www.youtube-nocookie.com/embed/${videoId}?autoplay=1`;
    player.title = title;
    player.allow = "autoplay; encrypted-media; picture-in-picture; fullscreen";
    player.allowFullscreen = true;
    block.replaceChildren(player);
    block.classList.add("video--playing");
  });

  block.replaceChildren(button);
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll(".video[data-youtube-id]").forEach(upgradeVideo);
});
