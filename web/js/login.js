// Hộp nhập mật khẩu truy cập (APP_ACCESS_TOKEN). Thay cho window.prompt(), vốn bị chặn trong trình duyệt
// nhúng của Zalo, Messenger, Facebook.

import { $ } from "./dom.js";

/**
 * Mở hộp mật khẩu và giữ nó mở cho tới khi tryLogin(password) báo thành công.
 * tryLogin trả về null khi đúng, hoặc câu báo lỗi để hiện dưới ô nhập.
 */
export function askPassword(tryLogin) {
  const dialog = $("#login-dialog");
  const form = $("#login-form");
  const field = $("#login-field");
  const input = $("#login-password");
  const error = $("#login-error");
  const submit = $("#login-submit");

  return new Promise((resolve) => {
    const showError = (message) => {
      field.classList.toggle("is-invalid", Boolean(message));
      error.textContent = message || "";
      error.hidden = !message;
    };

    // Không có mật khẩu thì không dùng được ứng dụng, nên Esc không đóng hộp.
    const onCancel = (event) => event.preventDefault();

    const onSubmit = async (event) => {
      event.preventDefault();
      const password = input.value.trim();
      if (!password) {
        showError("Hãy nhập mật khẩu.");
        input.focus();
        return;
      }
      submit.disabled = true;
      showError("");
      const message = await tryLogin(password);
      submit.disabled = false;
      if (message) {
        showError(message);
        input.select();
        return;
      }
      form.removeEventListener("submit", onSubmit);
      dialog.removeEventListener("cancel", onCancel);
      form.reset();
      dialog.close();
      resolve(true);
    };

    form.addEventListener("submit", onSubmit);
    dialog.addEventListener("cancel", onCancel);
    showError("");
    if (!dialog.open) dialog.showModal();
    input.focus();
  });
}
