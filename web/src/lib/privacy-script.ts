// Plain module (no "use client"), so the root layout (a server component) can inline the script as a string.

export const PRIVACY_KEY = "finresearch.privacy";

/** Runs before paint (inlined in <head>) so amounts are never shown unblurred while the page loads. */
export const PRIVACY_SCRIPT = `try{if(localStorage.getItem("${PRIVACY_KEY}")==="blur")document.documentElement.dataset.privacy="blur"}catch(e){}`;
