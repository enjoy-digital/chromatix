// Gowin USB 2.0 Device Controller (V3.4, Gowin V1.9.12.04) configuration for the Chromatic.
// Transfer types: 0: Bulk, 1: Isochronous, 2: Interrupt.
`define module_name USB_Device_Controller_Top
`define HSSUPPORT
`define DESC_PACKET_MAX64
`define UTMI
`define ENDPT1_IN_T_TYPE 2  // UVC video control/status (Interrupt).
`define ENDPT1_OUT_T_TYPE 0
`define ENDPT2_IN_T_TYPE 1  // UVC video streaming (Isochronous).
`define ENDPT2_OUT_T_TYPE 0
`define ENDPT3_IN_T_TYPE 0  // CDC data IN (Bulk).
`define ENDPT3_OUT_T_TYPE 0 // CDC data OUT (Bulk).
`define ENDPT4_IN_T_TYPE 2  // CDC notification (Interrupt).
`define ENDPT4_OUT_T_TYPE 0
`define ENDPT5_IN_T_TYPE 1  // UAC audio streaming (Isochronous).
`define ENDPT5_OUT_T_TYPE 0
`define ENDPT6_IN_T_TYPE 0
`define ENDPT6_OUT_T_TYPE 0
`define ENDPT7_IN_T_TYPE 0
`define ENDPT7_OUT_T_TYPE 0
`define ENDPT8_IN_T_TYPE 0
`define ENDPT8_OUT_T_TYPE 0
`define ENDPT9_IN_T_TYPE 0
`define ENDPT9_OUT_T_TYPE 0
`define ENDPT10_IN_T_TYPE 0
`define ENDPT10_OUT_T_TYPE 0
`define ENDPT11_IN_T_TYPE 0
`define ENDPT11_OUT_T_TYPE 0
`define ENDPT12_IN_T_TYPE 0
`define ENDPT12_OUT_T_TYPE 0
`define ENDPT13_IN_T_TYPE 0
`define ENDPT13_OUT_T_TYPE 0
`define ENDPT14_IN_T_TYPE 0
`define ENDPT14_OUT_T_TYPE 0
`define ENDPT15_IN_T_TYPE 0
`define ENDPT15_OUT_T_TYPE 0
