// A newer page can arrive while the user is reading backwards through the archive.
// Join on a shared message so a skipped poll or a session change cannot hide a gap.
export function mergeLatestMessages(previous, data) {
  if (!previous || previous.message_page?.session_id !== data.message_page?.session_id) return data;
  const overlap = previous.messages.findIndex(message => message.id === data.messages[0]?.id);
  if (overlap <= 0) return data;
  return {...data, messages:[...previous.messages.slice(0,overlap), ...data.messages],
    message_page:{...data.message_page, next_before_id:previous.message_page.next_before_id}};
}

export function prependMessages(state, cursor, page) {
  if (state?.message_page?.next_before_id !== cursor ||
      (page.messages.length && state.message_page.session_id !== page.message_page.session_id)) return state;
  return {...state, messages:[...page.messages, ...state.messages],
    message_page:{...state.message_page, next_before_id:page.message_page.next_before_id}};
}
