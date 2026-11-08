#!/bin/bash

i3nice::i3wm::winid_from_x() {
: param ' ~ <X11-Window-Id> ...'
: input "${1:?Window id expected}"

  < <(i3-msg -t get_tree) jq -r '.. | select(.window? == '"${1}"') | .id'
}

i3nice::xwinsel::get_id() {
: about 'Print i3 window Id from mouse click target'
: param ' ~ ...'
  local x11id

  x11id=$(xdotool selectwindow) && i3nice::i3wm::winid_from_x "$x11id"
}

i3nice::xwinsel::group_window() {
: about 'X set group helper (with yad)'
: param ' ~ ...'

  groupname=$(yad --title="i3nice prompt" --entry --entry-label="Group name") && con_id=$(i3nice::xwinsel::get_id) &&
  i3-nice-layout group --id=$con_id $groupname
}
