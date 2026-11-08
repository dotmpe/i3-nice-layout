#!/bin/bash

i3nice::i3wm::winid_from_x() {
: param ' ~ <X11-Window-Id> ...'
  < <(i3-msg -t get_tree) jq -r '.. | select(.window? == '"${1:?}"') | .id'
}

i3nice::xwinsel::get_id() {
: about 'Print i3 window Id from mouse click target'
: param ' ~ ...'
  local x11id
  x11id=$(xdotool selectwindow) && i3nice::i3wm::winid_from_x "$x11id"
}
