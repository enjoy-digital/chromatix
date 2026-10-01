# Doom sources (doomgeneric, vendored) and ChromatiX port, shared by Makefile (SoC) and Makefile.host.

DOOM_SRCS = dummy am_map doomdef doomstat dstrings d_event d_items d_iwad d_loop d_main d_mode \
	d_net f_finale f_wipe g_game hu_lib hu_stuff info i_cdmus i_endoom i_joystick i_scale i_sound \
	i_system i_timer memio m_argv m_bbox m_cheat m_config m_controls m_fixed m_menu m_misc m_random \
	p_ceilng p_doors p_enemy p_floor p_inter p_lights p_map p_maputl p_mobj p_plats p_pspr p_saveg \
	p_setup p_sight p_spec p_switch p_telept p_tick p_user r_bsp r_data r_draw r_main r_plane r_segs \
	r_sky r_things sha1 sounds statdump st_lib st_stuff s_sound tables v_video wi_stuff w_checksum \
	w_file w_main w_wad z_zone w_file_stdc i_input i_video doomgeneric

PORT_SRCS = chromatix_dg sound

DOOM_CFLAGS = -DDOOMGENERIC_DIRECT -DDOOMGENERIC_MEMWAD -DCMAP256 -DFEATURE_SOUND -DDOOMGENERIC_NO_SDL_MIXER \
	-DDOOMGENERIC_TIMEDEMO_HOOK -DDOOMGENERIC_FIXEDDIV32 -DDOOMGENERIC_LOWDETAIL_HALF -DDOOMGENERIC_ROW_SKIP -I../common \
	-DDOOMGENERIC_RESX=320 -DDOOMGENERIC_RESY=200 -Idoomgeneric -I.
